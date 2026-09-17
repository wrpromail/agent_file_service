from __future__ import annotations

import io
import mimetypes
from pathlib import Path
from typing import BinaryIO

from app.parsers.types import WILDCARD, FileClassification, FileKind


def _resolve_mime(filename: str, mime_type: str) -> str:
    return mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _pdf_subtype(file_data: BinaryIO) -> str:
    try:
        from pypdf import PdfReader

        if hasattr(file_data, "seek"):
            try:
                file_data.seek(0)
            except (io.UnsupportedOperation, OSError):
                pass
        reader = PdfReader(file_data)
        if not reader.pages:
            return "empty"
        text = (reader.pages[0].extract_text() or "").strip()
        if len(text) < 32:
            return "scanned"
        return "digital"
    except Exception:
        return "unknown"


def _image_subtype(filename: str) -> str:
    name = filename.lower()
    if any(token in name for token in ("scan", "scanned", "ocr")):
        return "document_scan"
    if any(token in name for token in ("screenshot", "screen", "capture")):
        return "screenshot"
    return "photo"


# 金山 WPS MIME hint（扩展名优先；勿用 application/vnd.ms-works——那是微软 Works）
_WPS_ET_MIMES = frozenset(
    {
        "application/wps-office.et",
        "application/wps-office.ett",
        "application/kbdspread",
    }
)
_WPS_DOC_MIMES = frozenset(
    {
        "application/wps-office.wps",
        "application/wps-office.wpt",
        "application/kswps",
    }
)
_WPS_PPT_MIMES = frozenset(
    {
        "application/wps-office.dps",
        "application/wps-office.dpt",
        "application/kdpresentation",
    }
)


def _spreadsheet_subtype(filename: str, mime_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".ett" or mime_type == "application/wps-office.ett":
        return "ett"
    if suffix == ".et" or mime_type in _WPS_ET_MIMES:
        return "et"
    if mime_type == "application/vnd.ms-excel" or suffix == ".xls":
        return "xls"
    return "xlsx"


def _normalise_file_data(file_data: BinaryIO | bytes) -> BinaryIO:
    """Return a seekable binary stream view over ``file_data``.

    Accepts either a ``bytes`` value (wrapped in ``BytesIO``) or a seekable
    file-like object (rewound to start). This lets classifiers and metadata
    extractors consume uploads streamed to a spooled temp file without the
    caller having to materialise the full payload.
    """
    if isinstance(file_data, (bytes, bytearray, memoryview)):
        return io.BytesIO(file_data)
    if hasattr(file_data, "seek"):
        try:
            file_data.seek(0)
        except (io.UnsupportedOperation, OSError):
            pass
    return file_data


def classify_file(filename: str, mime_type: str, file_data: BinaryIO | bytes) -> FileClassification:
    resolved_mime = _resolve_mime(filename, mime_type)
    suffix = Path(filename).suffix.lower()
    stream = _normalise_file_data(file_data)

    if resolved_mime == "application/pdf" or suffix == ".pdf":
        return FileClassification(
            kind=FileKind.PDF,
            subtype=_pdf_subtype(stream),
            mime_type=resolved_mime,
        )

    if resolved_mime.startswith("image/"):
        return FileClassification(
            kind=FileKind.IMAGE,
            subtype=_image_subtype(filename),
            mime_type=resolved_mime,
        )

    if (
        resolved_mime
        in {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        }
        or resolved_mime in _WPS_ET_MIMES
        or suffix in {".xlsx", ".xls", ".et", ".ett"}
    ):
        subtype = _spreadsheet_subtype(filename, resolved_mime)
        mime = resolved_mime
        if mime == "application/octet-stream" or not mime:
            if subtype == "et":
                mime = "application/wps-office.et"
            elif subtype == "ett":
                mime = "application/wps-office.ett"
        return FileClassification(
            kind=FileKind.SPREADSHEET,
            subtype=subtype,
            mime_type=mime,
        )

    # 金山 WPS 文字：扩展名优先；勿把 vnd.ms-works 当成金山格式
    if resolved_mime in _WPS_DOC_MIMES or suffix in {".wps", ".wpt"}:
        subtype = "wpt" if suffix == ".wpt" else "wps"
        mime = (
            resolved_mime
            if resolved_mime in _WPS_DOC_MIMES
            else ("application/wps-office.wpt" if subtype == "wpt" else "application/wps-office.wps")
        )
        return FileClassification(
            kind=FileKind.OFFICE,
            subtype=subtype,
            mime_type=mime,
        )

    if resolved_mime in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    } or suffix in {".docx", ".doc"}:
        return FileClassification(
            kind=FileKind.OFFICE,
            subtype="docx" if suffix == ".docx" else "doc",
            mime_type=resolved_mime if resolved_mime != "application/octet-stream" else (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if suffix == ".docx"
                else "application/msword"
            ),
        )

    # 金山 WPS 演示
    if resolved_mime in _WPS_PPT_MIMES or suffix in {".dps", ".dpt"}:
        subtype = "dpt" if suffix == ".dpt" else "dps"
        mime = (
            resolved_mime
            if resolved_mime in _WPS_PPT_MIMES
            else ("application/wps-office.dpt" if subtype == "dpt" else "application/wps-office.dps")
        )
        return FileClassification(
            kind=FileKind.OFFICE,
            subtype=subtype,
            mime_type=mime,
        )

    # 演示文稿：pptx 一等；ppt 旧二进制需 LibreOffice 预处理（layout）
    if resolved_mime in {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    } or suffix in {".pptx", ".ppt"}:
        if suffix == ".ppt" or resolved_mime == "application/vnd.ms-powerpoint":
            subtype = "ppt"
            mime = "application/vnd.ms-powerpoint"
        else:
            subtype = "pptx"
            mime = (
                resolved_mime
                if resolved_mime.startswith("application/")
                else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
        return FileClassification(
            kind=FileKind.OFFICE,
            subtype=subtype,
            mime_type=mime,
        )

    if (
        resolved_mime.startswith("text/")
        or resolved_mime in {"application/json", "application/ld+json"}
        or suffix in {".txt", ".md", ".csv", ".json"}
    ):
        if suffix == ".md":
            subtype = "markdown"
        elif suffix == ".csv":
            subtype = "csv"
        elif suffix == ".json" or "json" in resolved_mime:
            subtype = "json"
            if not resolved_mime.startswith("text/") and "json" not in resolved_mime:
                resolved_mime = "application/json"
            elif resolved_mime == "application/octet-stream":
                resolved_mime = "application/json"
        else:
            subtype = "plain"
        return FileClassification(
            kind=FileKind.TEXT,
            subtype=subtype,
            mime_type=resolved_mime if resolved_mime != "application/octet-stream" else (
                "application/json" if subtype == "json" else "text/plain"
            ),
        )

    if resolved_mime.startswith("audio/") or suffix in {
        ".wav",
        ".mp3",
        ".m4a",
        ".ogg",
        ".flac",
        ".aac",
        ".wma",
    }:
        subtype = suffix.lstrip(".") if suffix else "audio"
        return FileClassification(
            kind=FileKind.AUDIO,
            subtype=subtype or "audio",
            mime_type=resolved_mime if resolved_mime.startswith("audio/") else f"audio/{subtype or 'mpeg'}",
        )

    if resolved_mime.startswith("video/") or suffix in {
        ".mp4",
        ".webm",
        ".mov",
        ".avi",
        ".mkv",
        ".mpeg",
        ".mpg",
        ".m4v",
        ".3gp",
    }:
        subtype = suffix.lstrip(".") if suffix else "video"
        if subtype == "mpg":
            subtype = "mpeg"
        mime = resolved_mime if resolved_mime.startswith("video/") else _video_mime_for_suffix(subtype)
        return FileClassification(
            kind=FileKind.VIDEO,
            subtype=subtype or "video",
            mime_type=mime,
        )

    # 压缩包：zip / tar / tar.gz（解压策略见 archive_service）
    archive_cls = _classify_archive(filename, resolved_mime, suffix)
    if archive_cls is not None:
        return archive_cls

    return FileClassification(
        kind=FileKind.UNKNOWN,
        subtype=WILDCARD,
        mime_type=resolved_mime,
    )


def _classify_archive(filename: str, mime_type: str, suffix: str) -> FileClassification | None:
    name = filename.lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz") or suffix == ".tgz":
        return FileClassification(
            kind=FileKind.ARCHIVE,
            subtype="tar.gz",
            mime_type="application/gzip",
        )
    if name.endswith(".tar.bz2") or name.endswith(".tbz2"):
        return FileClassification(
            kind=FileKind.ARCHIVE,
            subtype="tar.bz2",
            mime_type="application/x-bzip2",
        )
    if suffix == ".tar" or mime_type in {"application/x-tar", "application/tar"}:
        return FileClassification(
            kind=FileKind.ARCHIVE,
            subtype="tar",
            mime_type="application/x-tar",
        )
    if suffix == ".zip" or mime_type in {
        "application/zip",
        "application/x-zip-compressed",
        "multipart/x-zip",
    }:
        return FileClassification(
            kind=FileKind.ARCHIVE,
            subtype="zip",
            mime_type="application/zip",
        )
    if suffix == ".gz" and not name.endswith(".tar.gz"):
        # bare .gz：不当作可展开 archive（单文件 gzip）
        return None
    return None


def _video_mime_for_suffix(subtype: str) -> str:
    mapping = {
        "mp4": "video/mp4",
        "m4v": "video/mp4",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "avi": "video/x-msvideo",
        "mkv": "video/x-matroska",
        "mpeg": "video/mpeg",
        "3gp": "video/3gpp",
    }
    return mapping.get(subtype, f"video/{subtype or 'mp4'}")