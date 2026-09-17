"""将 PDF / Office / 电子表格渲染为页图（PNG bytes）。

供 vision-pages **正文识别**后端使用（非版面 layout 引擎）。

- PDF：pypdfium2 直接栅格化
- Office / spreadsheet：LibreOffice → PDF，再栅格化
"""
from __future__ import annotations

import io
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_PDF_MIME = "application/pdf"
_PDF_SUFFIXES = {".pdf"}

# 需经 LibreOffice 转 PDF 的扩展名
_OFFICE_LIKE_SUFFIXES = {
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".odt",
    ".odp",
    ".ods",
    ".rtf",
}

_OFFICE_LIKE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/rtf",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.spreadsheet",
}


@dataclass(frozen=True)
class PageImage:
    """单页栅格化结果。"""

    page: int  # 1-based
    png_bytes: bytes
    mime_type: str = "image/png"
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class RasterizeResult:
    pages: tuple[PageImage, ...]
    source_pdf_path: str | None
    total_pages: int
    truncated: bool
    via_libreoffice: bool = False


def is_pdf_path(path: str | Path, *, filename: str = "", mime_type: str = "") -> bool:
    p = Path(path)
    suffix = (p.suffix or Path(filename or "").suffix).lower()
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime == _PDF_MIME or suffix in _PDF_SUFFIXES:
        return True
    # magic header
    try:
        with open(p, "rb") as fh:
            head = fh.read(5)
        return head == b"%PDF-"
    except OSError:
        return False


def needs_office_to_pdf(path: str | Path, *, filename: str = "", mime_type: str = "") -> bool:
    if is_pdf_path(path, filename=filename, mime_type=mime_type):
        return False
    suffix = (Path(path).suffix or Path(filename or "").suffix).lower()
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    return suffix in _OFFICE_LIKE_SUFFIXES or mime in _OFFICE_LIKE_MIMES


def _resize_png_if_needed(png_bytes: bytes, max_edge: int) -> tuple[bytes, int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max_edge > 0 and max(w, h) > max_edge:
            scale = max_edge / float(max(w, h))
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            w, h = img.size
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), w, h


def render_pdf_to_page_images(
    pdf_path: str | Path,
    *,
    max_pages: int = 20,
    scale: float = 2.0,
    max_edge_px: int = 1600,
) -> tuple[list[PageImage], int, bool]:
    """栅格化 PDF。返回 (pages, total_pages, truncated)。"""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "pypdfium2 is not installed; install with: pip install 'file-service[parsers]'"
        ) from exc

    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        total = len(doc)
        limit = total if max_pages <= 0 else min(total, max_pages)
        truncated = total > limit
        pages: list[PageImage] = []
        render_scale = scale if scale and scale > 0 else 2.0
        for index in range(limit):
            page = doc[index]
            try:
                bitmap = page.render(scale=render_scale)
                pil = bitmap.to_pil()
            finally:
                page.close()
            buf = io.BytesIO()
            pil.convert("RGB").save(buf, format="PNG")
            png_bytes, w, h = _resize_png_if_needed(buf.getvalue(), max_edge_px)
            pages.append(PageImage(page=index + 1, png_bytes=png_bytes, width=w, height=h))
        return pages, total, truncated
    finally:
        doc.close()


def convert_office_to_pdf(
    input_path: str | Path,
    *,
    output_dir: str | Path,
    settings: Settings | None = None,
) -> Path:
    """LibreOffice 将 Office/表格转为 PDF，返回 PDF 路径。"""
    from app.parsers.legacy_office import convert_with_libreoffice, resolve_soffice_binary

    settings = settings or get_settings()
    binary = resolve_soffice_binary(settings)
    if not binary:
        raise RuntimeError(
            "LibreOffice (soffice) not found; required to rasterize Office/spreadsheet for vision-pages. "
            "Install LibreOffice or set legacy_office_binary."
        )
    input_path = Path(input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"Office file not found: {input_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # LibreOffice 依赖输入扩展名；若临时文件无后缀，复制一份带后缀
    work_input = input_path
    temp_copy: Path | None = None
    if not input_path.suffix:
        raise RuntimeError(
            f"Office file has no extension (path={input_path.name}); cannot convert to PDF"
        )

    try:
        pdf_path = convert_with_libreoffice(
            str(work_input),
            "pdf",
            output_dir=str(output_dir),
            binary=binary,
            timeout=int(settings.legacy_office_timeout or 300),
        )
        return Path(pdf_path)
    finally:
        if temp_copy is not None:
            temp_copy.unlink(missing_ok=True)


def render_document_to_page_images(
    path: str | Path,
    *,
    filename: str = "",
    mime_type: str = "",
    max_pages: int | None = None,
    scale: float | None = None,
    max_edge_px: int | None = None,
    work_dir: str | Path | None = None,
    settings: Settings | None = None,
) -> RasterizeResult:
    """统一入口：PDF 或 Office/表格 → 页图列表。"""
    settings = settings or get_settings()
    path = Path(path)
    max_pages = (
        int(settings.vision_pages_max_pages)
        if max_pages is None
        else int(max_pages)
    )
    scale = float(settings.vision_pages_render_scale) if scale is None else float(scale)
    max_edge_px = (
        int(settings.vision_pages_max_edge_px) if max_edge_px is None else int(max_edge_px)
    )

    root = Path(work_dir or settings.vision_pages_work_dir or tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    session = tempfile.mkdtemp(prefix="vision-pages-", dir=str(root))
    session_path = Path(session)
    via_lo = False
    source_pdf: Path | None = None

    try:
        if is_pdf_path(path, filename=filename, mime_type=mime_type):
            source_pdf = path
        elif needs_office_to_pdf(path, filename=filename, mime_type=mime_type):
            # 保证输入有正确扩展名（parse_service 一般会带）
            work_input = path
            hint_suffix = Path(filename or "").suffix.lower()
            if hint_suffix and path.suffix.lower() != hint_suffix:
                work_input = session_path / f"input{hint_suffix}"
                shutil.copy2(path, work_input)
            source_pdf = convert_office_to_pdf(
                work_input, output_dir=session_path / "lo-out", settings=settings
            )
            via_lo = True
        else:
            raise RuntimeError(
                f"vision-pages cannot rasterize this file type "
                f"(filename={filename or path.name!r}, mime={mime_type!r}); "
                f"supported: PDF, Office, spreadsheet"
            )

        pages, total, truncated = render_pdf_to_page_images(
            source_pdf,
            max_pages=max_pages,
            scale=scale,
            max_edge_px=max_edge_px,
        )
        if not pages:
            raise RuntimeError("Document rasterize produced zero pages")

        # 复制页图到结果前先读入内存；session 可删
        return RasterizeResult(
            pages=tuple(pages),
            source_pdf_path=str(source_pdf) if via_lo else None,
            total_pages=total,
            truncated=truncated,
            via_libreoffice=via_lo,
        )
    finally:
        shutil.rmtree(session_path, ignore_errors=True)
