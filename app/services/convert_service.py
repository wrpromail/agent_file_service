"""file_convert 兼容层：同步转换，产物入库为本服务引用。"""
from __future__ import annotations

import base64
import io
import logging
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.storage import StorageBackend, storage_key_from_uri
from app.models.parse_result import ParseResult
from app.services.file_service import upload_file
from app.services.parse_service import (
    execute_parse,
    find_cached_parse,
    get_or_create_parse,
    load_structured_content,
    resolve_parser_for_profile,
)
from app.services.url_import_service import UrlImportError, validate_url_for_import

logger = logging.getLogger(__name__)

OFFICE_FORMATS = frozenset({"doc", "docx", "ppt", "pptx", "xls", "xlsx"})
EXTRACT_FORMATS = OFFICE_FORMATS | {"pdf"}
IMAGE_INPUT_FORMATS = frozenset({"doc", "docx", "pdf"})

_MIME_BY_FORMAT = {
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "md": "text/markdown",
    "zip": "application/zip",
}


class ConvertError(Exception):
    def __init__(self, message: str, http_status: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def raise_as_http(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, ConvertError):
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    if isinstance(exc, UrlImportError):
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


def _session_id(settings: Settings) -> str:
    return (settings.convert_compat_session_id or "convert-compat").strip() or "convert-compat"


def _public_url(public_base_url: str, reference_id: uuid.UUID) -> str:
    base = (public_base_url or "").rstrip("/")
    return f"{base}/v1/files/{reference_id}/download"


def _maybe_b64(data: bytes, settings: Settings) -> str | None:
    limit = int(settings.convert_compat_max_inline_bytes or 0)
    if limit > 0 and len(data) > limit:
        return None
    return base64.b64encode(data).decode("ascii")


def _filename_from_url(url: str, file_format: str) -> str:
    path = urlparse(url).path or ""
    name = PurePosixPath(path).name
    if name and "." in name:
        return name
    return f"unknown.{file_format}"


def download_convert_url(
    file_url: str,
    *,
    settings: Settings | None = None,
) -> tuple[bytes, str, str | None]:
    """Fetch file_url. Private hosts allowed when convert_compat_allow_private_urls."""
    import httpx

    settings = settings or get_settings()
    raw = (file_url or "").strip()
    if not raw:
        raise ConvertError("file_url is required", 400)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ConvertError("Only http/https URLs are allowed", 400)
    if not settings.convert_compat_allow_private_urls:
        validate_url_for_import(raw, settings)

    timeout = float(settings.from_url_timeout or 60.0)
    max_upload = int(settings.max_upload_bytes or 0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            with client.stream("GET", raw) as resp:
                if resp.status_code >= 400:
                    raise ConvertError(
                        f"Remote returned HTTP {resp.status_code}",
                        502 if resp.status_code >= 500 else resp.status_code,
                    )
                content_type = resp.headers.get("content-type")
                if content_type and ";" in content_type:
                    content_type = content_type.split(";", 1)[0].strip()
                buf = io.BytesIO()
                total = 0
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_upload > 0 and total > max_upload:
                        raise ConvertError(
                            f"Remote file exceeds max_upload_bytes={max_upload}",
                            413,
                        )
                    buf.write(chunk)
    except httpx.HTTPError as exc:
        raise ConvertError(f"HTTP fetch failed: {exc}", 502) from exc
    return buf.getvalue(), _filename_from_url(raw, "bin"), content_type


def _ingest_bytes(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    data: bytes,
    filename: str,
    content_type: str,
    convert_kind: str,
    extra_meta: dict[str, Any] | None = None,
    settings: Settings,
) -> Any:
    meta = {
        "convert_compat": True,
        "convert_kind": convert_kind,
        **(extra_meta or {}),
    }
    return upload_file(
        db,
        storage,
        tenant_id=tenant_id,
        file_obj=io.BytesIO(data),
        filename=filename,
        content_type=content_type,
        session_id=_session_id(settings),
        agent_id="convert-compat",
        uploaded_by="convert-compat",
        custom_metadata=meta,
        auto_parse=False,
        expand_archives=False,
    )


def _wait_layout(
    db: Session,
    *,
    content_hash: str,
    filename: str,
    mime_type: str,
    settings: Settings,
) -> tuple[ParseResult, bool]:
    parser, _ = resolve_parser_for_profile(
        db,
        content_hash=content_hash,
        filename=filename,
        mime_type=mime_type,
        file_data=b"",
        profile="layout",
    )
    cached = find_cached_parse(
        db,
        content_hash,
        profile="layout",
        parser_name=parser.name,
        parser_version=parser.version,
    )
    if cached is not None:
        return cached, True

    row, should_run = get_or_create_parse(
        db,
        content_hash,
        profile="layout",
        parser_name=parser.name,
        parser_version=parser.version,
    )
    if should_run or row.status in {"pending", "processing", "failed"}:
        _run_parse_blocking(db, row.id, settings=settings)
        row = db.get(ParseResult, row.id)
        if row is None:
            raise ConvertError("Parse result disappeared after conversion")
    return row, False


def _run_parse_blocking(db: Session, parse_result_id: uuid.UUID, *, settings: Settings) -> None:
    backend = (settings.parse_task_backend or "local").strip().lower()
    if settings.parse_sync or backend != "celery":
        execute_parse(db, parse_result_id, settings=settings)
        db.flush()
        return
    db.commit()
    from app.tasks.parse_tasks import run_layout_parse_task

    timeout = float(settings.convert_compat_timeout or 300)
    run_layout_parse_task.apply_async(args=[str(parse_result_id)]).get(timeout=timeout)
    db.expire_all()


def _markdown_from_parse(storage: StorageBackend, row: ParseResult) -> str:
    if row.status != "completed":
        raise ConvertError(row.error or f"conversion failed status={row.status}", 500)
    content = load_structured_content(storage, row)
    md = content.metadata.get("markdown")
    if isinstance(md, str) and md:
        return md
    parts = [b.text for b in content.blocks if b.text]
    return "\n\n".join(parts)


def _touch_cache_summary(
    row: ParseResult,
    *,
    file_format: str,
    file_name: str,
    cache_hit: bool,
) -> None:
    summary = dict(row.summary or {})
    summary["convert_compat"] = True
    summary["convert_format"] = file_format
    summary["file_name"] = file_name
    count = int(summary.get("access_count") or 0)
    summary["access_count"] = count + 1 if cache_hit else max(count, 1)
    summary["last_access"] = datetime.now(timezone.utc).isoformat()
    row.summary = summary


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ConvertError(
            "pypdf is not installed; install with: pip install 'file-service[metadata]'",
            500,
        ) from exc
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def office_to_markdown(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    file_url: str,
    file_format: str,
    public_base_url: str = "",
) -> dict[str, Any]:
    started = time.time()
    fmt = (file_format or "").lower().lstrip(".")
    if fmt not in OFFICE_FORMATS:
        raise ConvertError(f"不支持的文件格式: {file_format}", 400)
    settings = get_settings()
    data, url_name, _ct = download_convert_url(file_url, settings=settings)
    filename = url_name if "." in url_name else f"{url_name}.{fmt}"
    mime = _MIME_BY_FORMAT[fmt]
    uploaded = _ingest_bytes(
        db,
        storage,
        tenant_id=tenant_id,
        data=data,
        filename=filename,
        content_type=mime,
        convert_kind="office-to-markdown",
        extra_meta={"convert_format": fmt, "source_url": file_url[:500]},
        settings=settings,
    )
    row, cache_hit = _wait_layout(
        db,
        content_hash=uploaded.content_hash,
        filename=filename,
        mime_type=mime,
        settings=settings,
    )
    markdown = _markdown_from_parse(storage, row)
    _touch_cache_summary(row, file_format=fmt, file_name=filename, cache_hit=cache_hit)
    db.flush()
    elapsed = time.time() - started
    kind = "cache hit" if cache_hit else "processed"
    return {
        "message": (
            f"{fmt.upper()} to Markdown conversion completed in {elapsed:.2f}s ({kind})"
        ),
        "markdown_content": markdown,
        "task_id": str(uploaded.reference_id),
        "file_format": fmt,
        "processing_time": round(elapsed, 4),
        "cache_hit": cache_hit,
        "reference_id": str(uploaded.reference_id),
    }


def extract_text(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    file_url: str,
    file_format: str,
    public_base_url: str = "",
) -> dict[str, Any]:
    started = time.time()
    fmt = (file_format or "").lower().lstrip(".")
    if fmt not in EXTRACT_FORMATS:
        raise ConvertError(f"不支持的文件格式: {file_format}", 400)
    settings = get_settings()
    data, url_name, _ct = download_convert_url(file_url, settings=settings)
    filename = url_name if "." in url_name else f"{url_name}.{fmt}"
    mime = _MIME_BY_FORMAT[fmt]
    uploaded = _ingest_bytes(
        db,
        storage,
        tenant_id=tenant_id,
        data=data,
        filename=filename,
        content_type=mime,
        convert_kind="extract-text",
        extra_meta={"convert_format": fmt, "source_url": file_url[:500]},
        settings=settings,
    )
    if fmt == "pdf":
        text, cache_hit = _pdf_text_with_cache(
            db,
            storage,
            content_hash=uploaded.content_hash,
            data=data,
            filename=filename,
        )
    else:
        row, cache_hit = _wait_layout(
            db,
            content_hash=uploaded.content_hash,
            filename=filename,
            mime_type=mime,
            settings=settings,
        )
        text = _markdown_from_parse(storage, row)
        _touch_cache_summary(row, file_format=fmt, file_name=filename, cache_hit=cache_hit)
        db.flush()
    elapsed = time.time() - started
    return {
        "message": f"Text extraction completed in {elapsed:.2f}s",
        "text_content": text,
        "task_id": str(uploaded.reference_id),
        "file_format": fmt,
        "processing_time": round(elapsed, 4),
        "cache_hit": cache_hit,
        "reference_id": str(uploaded.reference_id),
    }


def _pdf_text_with_cache(
    db: Session,
    storage: StorageBackend,
    *,
    content_hash: str,
    data: bytes,
    filename: str,
) -> tuple[str, bool]:
    parser_name = "pypdf-text"
    parser_version = "0.1.0"
    cached = find_cached_parse(
        db,
        content_hash,
        profile="layout",
        parser_name=parser_name,
        parser_version=parser_version,
    )
    if cached is not None and cached.result_uri:
        raw = storage.get_object(storage_key_from_uri(cached.result_uri))
        _touch_cache_summary(cached, file_format="pdf", file_name=filename, cache_hit=True)
        db.flush()
        return raw.decode("utf-8"), True

    text = _extract_pdf_text(data)
    row, _should_run = get_or_create_parse(
        db,
        content_hash,
        profile="layout",
        parser_name=parser_name,
        parser_version=parser_version,
    )
    from app.core.storage import parse_result_storage_key

    key = parse_result_storage_key(content_hash, parser_name, "layout")
    uri = storage.put_object(key, io.BytesIO(text.encode("utf-8")), "text/plain")
    row.status = "completed"
    row.result_uri = uri
    row.completed_at = datetime.now(timezone.utc)
    row.error = None
    _touch_cache_summary(row, file_format="pdf", file_name=filename, cache_hit=False)
    db.flush()
    return text, False


def markdown_to_pdf(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    markdown_content: str,
    public_base_url: str = "",
) -> dict[str, Any]:
    if not (markdown_content or "").strip():
        raise ConvertError("markdown_content is required", 400)
    settings = get_settings()
    from app.parsers.markdown_pdf import convert_markdown_to_pdf

    pdf_data = convert_markdown_to_pdf(markdown_content, settings=settings)
    uploaded = _ingest_bytes(
        db,
        storage,
        tenant_id=tenant_id,
        data=pdf_data,
        filename="converted.pdf",
        content_type="application/pdf",
        convert_kind="markdown-to-pdf",
        settings=settings,
    )
    encoded = _maybe_b64(pdf_data, settings)
    ref = str(uploaded.reference_id)
    return {
        "message": "PDF conversion completed",
        "pdf_url": _public_url(public_base_url, uploaded.reference_id),
        "pdf_base64": encoded,
        "file_path": None,
        "task_id": ref,
        "reference_id": ref,
    }


def document_to_images(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    file_url: str,
    file_format: str,
    image_format: str = "jpeg",
    quality: int = 80,
    max_width: int | None = 1800,
    dpi: int = 220,
    delivery_mode: str = "storage",
    pack_mode: str = "zip",
    public_base_url: str = "",
) -> dict[str, Any]:
    fmt = (file_format or "").lower().lstrip(".")
    if fmt not in IMAGE_INPUT_FORMATS:
        raise ConvertError(f"不支持的输入格式: {file_format}", 400)
    img_fmt = (image_format or "jpeg").lower()
    if img_fmt == "jpg":
        img_fmt = "jpeg"
    if img_fmt not in {"jpeg", "png"}:
        raise ConvertError(f"不支持的图片格式: {image_format}", 400)
    settings = get_settings()
    data, url_name, _ct = download_convert_url(file_url, settings=settings)
    filename = url_name if "." in (url_name or "") else f"source.{fmt}"
    mime = _MIME_BY_FORMAT[fmt]
    scale = max(0.5, float(dpi) / 72.0)
    max_edge = int(max_width) if max_width else 0

    suffix = Path(filename).suffix or f".{fmt}"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="fs-convert-img-")
    try:
        tmp.write(data)
        tmp.close()
        from app.parsers.document_rasterize import render_document_to_page_images

        raster = render_document_to_page_images(
            tmp.name,
            filename=filename,
            mime_type=mime,
            max_pages=0,
            scale=scale,
            max_edge_px=max_edge or 4096,
            settings=settings,
        )
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    pages = _encode_page_images(raster.pages, img_fmt, quality)
    task_id = str(uuid.uuid4())
    images_out: list[dict[str, Any]] = []
    archive_url = None
    archive_b64 = None
    archive_name = None
    if pack_mode == "zip":
        archive_bytes = _zip_pages(pages)
        archive_name = "document-pages.zip"
        if delivery_mode == "storage":
            uploaded = _ingest_bytes(
                db,
                storage,
                tenant_id=tenant_id,
                data=archive_bytes,
                filename=archive_name,
                content_type="application/zip",
                convert_kind="document-to-images",
                extra_meta={"convert_format": fmt, "pack_mode": "zip"},
                settings=settings,
            )
            task_id = str(uploaded.reference_id)
            archive_url = _public_url(public_base_url, uploaded.reference_id)
        archive_b64 = _maybe_b64(archive_bytes, settings)
        images_out = [_page_meta(p) for p in pages]
    else:
        for page in pages:
            item = _page_meta(page)
            if delivery_mode == "storage":
                uploaded = _ingest_bytes(
                    db,
                    storage,
                    tenant_id=tenant_id,
                    data=page["bytes"],
                    filename=page["filename"],
                    content_type=f"image/{img_fmt}",
                    convert_kind="document-to-images",
                    extra_meta={"convert_format": fmt, "pack_mode": "list"},
                    settings=settings,
                )
                item["download_url"] = _public_url(public_base_url, uploaded.reference_id)
                task_id = str(uploaded.reference_id)
            encoded = _maybe_b64(page["bytes"], settings)
            if encoded:
                item["base64_data"] = encoded
            images_out.append(item)
    return {
        "message": "Document to image conversion completed",
        "task_id": task_id,
        "delivery_mode": delivery_mode,
        "pack_mode": pack_mode,
        "page_count": len(pages),
        "archive_filename": archive_name,
        "archive_url": archive_url,
        "archive_base64": archive_b64,
        "images": images_out,
        "reference_id": task_id,
    }


def _encode_page_images(pages, image_format: str, quality: int) -> list[dict[str, Any]]:
    from PIL import Image

    ext = "jpg" if image_format == "jpeg" else "png"
    results: list[dict[str, Any]] = []
    for page in pages:
        img = Image.open(io.BytesIO(page.png_bytes))
        if image_format == "jpeg" and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        if image_format == "jpeg":
            img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        else:
            compress = max(0, min(9, round((100 - quality) / 10)))
            img.save(buf, format="PNG", optimize=True, compress_level=compress)
        payload = buf.getvalue()
        results.append(
            {
                "page": page.page,
                "width": page.width,
                "height": page.height,
                "size": len(payload),
                "filename": f"page-{page.page:03d}.{ext}",
                "bytes": payload,
            }
        )
    return results


def _page_meta(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page": page["page"],
        "width": page["width"],
        "height": page["height"],
        "size": page["size"],
        "filename": page["filename"],
        "download_url": None,
        "base64_data": None,
    }


def _zip_pages(pages: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for page in pages:
            archive.writestr(page["filename"], page["bytes"])
    return buf.getvalue()


def generate_excel(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    files: list[dict[str, Any]] | None = None,
    source: list[dict[str, Any]] | None = None,
    file_name: str | None = None,
    public_base_url: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    files_to_process = list(files or [])
    if source:
        default_name = _ensure_xlsx(file_name or "output.xlsx")
        sheets = []
        for sheet in source:
            data = sheet.get("data") or {}
            columns = data.get("columns") or []
            rows = data.get("rows") or []
            mapping = {col["key"]: col["name"] for col in columns if "key" in col and "name" in col}
            sheets.append(
                {
                    "sheet_name": sheet["sheet_name"],
                    "data": rows,
                    "mapping": mapping or None,
                }
            )
        files_to_process.append({"filename": default_name, "sheets": sheets})
    if not files_to_process:
        raise ConvertError("files 与 source 至少需要提供一种", 400)

    file_urls: dict[str, str] = {}
    file_ids: dict[str, str] = {}
    last_b64: str | None = None
    task_id = str(uuid.uuid4())
    for file_config in files_to_process:
        filename = _ensure_xlsx(file_config["filename"])
        xlsx_bytes = build_xlsx_bytes(file_config["sheets"])
        uploaded = _ingest_bytes(
            db,
            storage,
            tenant_id=tenant_id,
            data=xlsx_bytes,
            filename=filename,
            content_type=_MIME_BY_FORMAT["xlsx"],
            convert_kind="generate-excel",
            extra_meta={"filename": filename},
            settings=settings,
        )
        task_id = str(uploaded.reference_id)
        file_urls[filename] = _public_url(public_base_url, uploaded.reference_id)
        file_ids[filename] = str(uploaded.reference_id)
        last_b64 = _maybe_b64(xlsx_bytes, settings)
    return {
        "message": "Excel generation completed",
        "task_id": task_id,
        "file_urls": file_urls,
        "file_ids": file_ids,
        "reference_id": task_id,
        "file_base64": last_b64,
    }


def _ensure_xlsx(name: str) -> str:
    name = (name or "output").strip() or "output"
    return name if name.lower().endswith(".xlsx") else f"{name}.xlsx"


def build_xlsx_bytes(sheets: list[dict[str, Any]]) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover
        raise ConvertError(
            "openpyxl is not installed; install with: pip install 'file-service[parsers]'",
            500,
        ) from exc
    wb = Workbook()
    default = wb.active
    first = True
    for sheet in sheets:
        title = str(sheet.get("sheet_name") or "Sheet1")[:31]
        ws = default if first else wb.create_sheet(title=title)
        if first:
            ws.title = title
            first = False
        rows = list(sheet.get("data") or [])
        mapping: dict[str, str] | None = sheet.get("mapping")
        if mapping:
            headers = list(mapping.values())
            keys = list(mapping.keys())
        elif rows:
            keys = list(rows[0].keys())
            headers = keys
        else:
            keys, headers = [], []
        if headers:
            ws.append(headers)
        for row in rows:
            ws.append([row.get(key) for key in keys])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def cache_stats(db: Session) -> dict[str, Any]:
    rows = db.scalars(select(ParseResult)).all()
    cached = [r for r in rows if (r.summary or {}).get("convert_compat")]
    by_fmt: dict[str, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    for row in cached:
        fmt = str((row.summary or {}).get("convert_format") or "unknown")
        stat = by_fmt.setdefault(fmt, {"format": fmt, "count": 0, "total_access": 0})
        stat["count"] += 1
        stat["total_access"] += int((row.summary or {}).get("access_count") or 0)
        recent.append(
            {
                "file_name": (row.summary or {}).get("file_name"),
                "format": fmt,
                "access_count": int((row.summary or {}).get("access_count") or 0),
                "last_access": (row.summary or {}).get("last_access")
                or (row.completed_at.isoformat() if row.completed_at else None),
            }
        )
    recent.sort(key=lambda x: x.get("last_access") or "", reverse=True)
    return {
        "total_cached_files": len(cached),
        "format_stats": list(by_fmt.values()),
        "recent_access": recent[:10],
    }


def clear_old_cache(db: Session, storage: StorageBackend, days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = 0
    rows = db.scalars(select(ParseResult)).all()
    for row in rows:
        if not (row.summary or {}).get("convert_compat"):
            continue
        stamp = row.completed_at or row.created_at
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp >= cutoff:
            continue
        if row.result_uri:
            try:
                storage.delete_object(storage_key_from_uri(row.result_uri))
            except Exception:
                logger.debug("convert cache object delete failed", exc_info=True)
        db.delete(row)
        deleted += 1
    db.flush()
    return deleted


def dispatch_or_local(
    op: str,
    db: Session,
    storage: StorageBackend,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run conversion in-process, or wait on a Celery worker when configured."""
    settings = get_settings()
    backend = (settings.parse_task_backend or "local").strip().lower()
    if settings.parse_sync or backend != "celery":
        return run_convert_op(op, db, storage, **kwargs)
    from app.tasks.convert_tasks import run_convert_task

    timeout = float(settings.convert_compat_timeout or 300)
    return run_convert_task.apply_async(args=[op], kwargs=kwargs).get(timeout=timeout)


def run_convert_op(
    op: str,
    db: Session,
    storage: StorageBackend,
    **kwargs: Any,
) -> dict[str, Any]:
    ops = {
        "office_to_markdown": office_to_markdown,
        "extract_text": extract_text,
        "markdown_to_pdf": markdown_to_pdf,
        "document_to_images": document_to_images,
        "generate_excel": generate_excel,
    }
    fn = ops.get(op)
    if fn is None:
        raise ConvertError(f"unknown convert op: {op}", 400)
    return fn(db, storage, **kwargs)
