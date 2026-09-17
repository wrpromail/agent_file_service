from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.storage import (
    StorageBackend,
    blob_storage_key,
    parse_result_storage_key,
    storage_key_from_uri,
)
from app.models.blob import Blob
from app.models.parse_result import ParseResult
from app.parsers.base import ParseContext
from app.parsers.base import Parser
from app.parsers.classification_store import load_classification
from app.parsers.registry import get_registry
from app.parsers.types import FileClassification, ProcessingLevel
from app.schemas.content import StructuredContent

MIME_TO_SUFFIX = {
    "text/plain": ".txt",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".ppt",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "video/mpeg": ".mpeg",
    "video/3gpp": ".3gp",
}

SUPPORTED_PARSE_PROFILES = frozenset(
    {
        ProcessingLevel.LAYOUT.value,
        ProcessingLevel.OCR.value,
        ProcessingLevel.DEEP.value,
        ProcessingLevel.TABLES.value,
    }
)


def resolve_parser_for_profile(
    db: Session,
    *,
    content_hash: str,
    filename: str,
    mime_type: str,
    file_data: BinaryIO | bytes,
    profile: str,
) -> tuple[Parser, FileClassification]:
    registry = get_registry()
    classification = load_classification(
        db,
        content_hash=content_hash,
        filename=filename,
        mime_type=mime_type,
        file_data=file_data,
    )
    parser = registry.get_parser_for_classification(classification, profile)
    if parser is None:
        raise ValueError(f"No parser for profile={profile}, kind={classification.kind}, subtype={classification.subtype}")
    return parser, classification


def find_cached_parse(
    db: Session,
    content_hash: str,
    *,
    profile: str,
    parser_name: str,
    parser_version: str,
) -> ParseResult | None:
    return db.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == content_hash,
            ParseResult.parser_name == parser_name,
            ParseResult.parser_version == parser_version,
            ParseResult.profile == profile,
            ParseResult.status == "completed",
        )
    )


def get_or_create_parse(
    db: Session,
    content_hash: str,
    *,
    profile: str,
    parser_name: str,
    parser_version: str,
    force: bool = False,
) -> tuple[ParseResult, bool]:
    """Return parse row and whether a new job should run."""
    from sqlalchemy.exc import IntegrityError

    existing = db.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == content_hash,
            ParseResult.parser_name == parser_name,
            ParseResult.parser_version == parser_version,
            ParseResult.profile == profile,
        )
    )
    if existing and existing.status == "completed" and not force:
        return existing, False
    if existing and existing.status in {"pending", "processing"} and not force:
        return existing, False

    if existing and force:
        if existing.status == "processing":
            raise ValueError(
                "Parse is already in progress; wait for completion before force re-parse."
            )
        existing.status = "pending"
        existing.error = None
        existing.result_uri = None
        existing.completed_at = None
        db.flush()
        return existing, True

    if existing:
        return existing, existing.status in {"pending", "failed"}

    row = ParseResult(
        content_hash=content_hash,
        parser_name=parser_name,
        parser_version=parser_version,
        profile=profile,
        status="pending",
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row, True
    except IntegrityError:
        existing = db.scalar(
            select(ParseResult).where(
                ParseResult.content_hash == content_hash,
                ParseResult.parser_name == parser_name,
                ParseResult.parser_version == parser_version,
                ParseResult.profile == profile,
            )
        )
        if existing is None:
            raise
        if existing.status == "completed" and not force:
            return existing, False
        if existing.status in {"pending", "processing"} and not force:
            return existing, False
        return existing, existing.status in {"pending", "failed"}


def run_background_parse_task(parse_result_id: uuid.UUID, settings: Settings) -> None:
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        execute_parse(db, parse_result_id, settings=settings)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _enqueue_parse_after_commit(parse_result_id: uuid.UUID) -> None:
    """Celery enqueue — called via BackgroundTasks after the request commits."""
    from app.tasks.parse_tasks import run_layout_parse_task

    run_layout_parse_task.delay(str(parse_result_id))


def _dispatch_async_parse(
    parse_result_id: uuid.UUID,
    settings: Settings,
    background_tasks: BackgroundTasks | None,
) -> None:
    """Route async work: celery worker (Docker) or in-process (local dev)."""
    backend = (settings.parse_task_backend or "local").strip().lower()
    if backend == "celery":
        # FastAPI BackgroundTasks run after response; route already db.commit()'d.
        if background_tasks is not None:
            background_tasks.add_task(_enqueue_parse_after_commit, parse_result_id)
        else:
            _enqueue_parse_after_commit(parse_result_id)
        return

    if background_tasks is not None:
        background_tasks.add_task(run_background_parse_task, parse_result_id, settings)
    else:
        import threading

        thread = threading.Thread(
            target=run_background_parse_task,
            args=(parse_result_id, settings),
            daemon=True,
        )
        thread.start()


def schedule_profile_parse(
    db: Session,
    content_hash: str,
    *,
    filename: str,
    mime_type: str,
    file_data: BinaryIO | bytes,
    profile: str = ProcessingLevel.LAYOUT.value,
    force: bool = False,
    settings: Settings | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> ParseResult:
    settings = settings or get_settings()
    parser, _ = resolve_parser_for_profile(
        db,
        content_hash=content_hash,
        filename=filename,
        mime_type=mime_type,
        file_data=file_data,
        profile=profile,
    )
    row, should_run = get_or_create_parse(
        db,
        content_hash,
        profile=profile,
        parser_name=parser.name,
        parser_version=parser.version,
        force=force,
    )
    if should_run:
        from app.core.metrics import inc_metric

        inc_metric("parse_started_total", profile=profile)
        if settings.parse_sync:
            execute_parse(db, row.id, settings=settings)
        else:
            _dispatch_async_parse(row.id, settings, background_tasks)
    return row


def schedule_deep_parse(
    db: Session,
    content_hash: str,
    *,
    filename: str,
    mime_type: str,
    file_data: BinaryIO | bytes,
    force: bool = False,
    settings: Settings | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> ParseResult:
    return schedule_profile_parse(
        db,
        content_hash,
        filename=filename,
        mime_type=mime_type,
        file_data=file_data,
        profile=ProcessingLevel.DEEP.value,
        force=force,
        settings=settings,
        background_tasks=background_tasks,
    )


def schedule_layout_parse(
    db: Session,
    content_hash: str,
    *,
    filename: str,
    mime_type: str,
    file_data: BinaryIO | bytes,
    force: bool = False,
    settings: Settings | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> ParseResult:
    return schedule_profile_parse(
        db,
        content_hash,
        filename=filename,
        mime_type=mime_type,
        file_data=file_data,
        profile=ProcessingLevel.LAYOUT.value,
        force=force,
        settings=settings,
        background_tasks=background_tasks,
    )


def _download_blob_to_temp(storage: StorageBackend, blob: Blob, suffix: str = ".bin") -> str:
    """Stream blob bytes from object storage into a temporary file and return its path.

    Avoids loading the full blob into memory; the caller must unlink the returned
    path once done.
    """
    key = blob_storage_key(blob.content_hash)
    import tempfile

    temp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="fs-parse-")
    try:
        storage.stream_object(key, temp)
        temp.flush()
    except Exception:
        temp.close()
        from pathlib import Path

        Path(temp.name).unlink(missing_ok=True)
        raise
    finally:
        temp.close()
    return temp.name


def execute_parse(
    db: Session,
    parse_result_id: uuid.UUID,
    *,
    storage: StorageBackend | None = None,
    settings: Settings | None = None,
) -> ParseResult:
    from sqlalchemy import update

    settings = settings or get_settings()
    if storage is None:
        from app.core.storage import get_storage

        storage = get_storage()

    row = db.get(ParseResult, parse_result_id)
    if row is None:
        raise ValueError(f"ParseResult not found: {parse_result_id}")

    claimed = db.execute(
        update(ParseResult)
        .where(
            ParseResult.id == parse_result_id,
            ParseResult.status.in_(("pending", "failed")),
        )
        .values(status="processing", error=None, processing_started_at=datetime.now(timezone.utc))
    )
    if claimed.rowcount == 0:
        db.refresh(row)
        return row
    db.flush()
    db.refresh(row)

    blob = db.get(Blob, row.content_hash)
    if blob is None:
        row.status = "failed"
        row.error = "Blob not found"
        row.completed_at = datetime.now(timezone.utc)
        db.flush()
        return row

    try:
        filename = (
            _filename_from_metadata(db, blob.content_hash)
            or _filename_from_reference(db, blob.content_hash)
            or blob.content_hash
        )
        # 下载时尽量带对扩展名（LibreOffice 依赖扩展名）
        suffix = _suffix_for_blob(blob.mime_type, filename_hint=filename)
        temp_path = _download_blob_to_temp(storage, blob, suffix=suffix)
        legacy_result = None
        try:
            classification = load_classification(
                db,
                content_hash=blob.content_hash,
                filename=filename,
                mime_type=blob.mime_type,
                file_data=b"",
            )
            registry = get_registry()
            parser = registry.get_parser_for_classification(classification, row.profile)
            if parser is None:
                row.status = "failed"
                row.error = (
                    f"No parser for profile={row.profile}, kind={classification.kind}, "
                    f"subtype={classification.subtype}"
                )
                row.completed_at = datetime.now(timezone.utc)
                db.flush()
                return row

            parse_path = temp_path
            # layout/ocr/tables：旧式 Office 先转 OOXML（派生产物缓存，不建第二引用）
            if row.profile in {
                ProcessingLevel.LAYOUT.value,
                ProcessingLevel.OCR.value,
                ProcessingLevel.TABLES.value,
            }:
                from app.parsers.legacy_office import (
                    cleanup_legacy_normalize,
                    ensure_normalized_office,
                )

                legacy_result = ensure_normalized_office(
                    temp_path,
                    filename=filename,
                    mime_type=blob.mime_type,
                    content_hash=blob.content_hash,
                    storage=storage,
                    settings=settings,
                )
                if legacy_result is not None:
                    parse_path = legacy_result.path

            context = ParseContext(
                content_hash=blob.content_hash,
                mime_type=blob.mime_type,
                filename=filename,
                size_bytes=blob.size_bytes,
                classification=classification,
            )
            structured: StructuredContent = parser.parse(parse_path, context)
            if legacy_result is not None:
                meta = dict(structured.metadata or {})
                meta["legacy_converted"] = True
                meta["legacy_source_format"] = legacy_result.source_format
                meta["legacy_target_format"] = legacy_result.target_format
                meta["legacy_converter"] = legacy_result.converter
                meta["legacy_converter_version"] = legacy_result.converter_version
                meta["legacy_cache_hit"] = legacy_result.cache_hit
                structured = structured.model_copy(update={"metadata": meta})
        finally:
            Path(temp_path).unlink(missing_ok=True)
            if legacy_result is not None:
                from app.parsers.legacy_office import cleanup_legacy_normalize

                cleanup_legacy_normalize(legacy_result)

        result_key = parse_result_storage_key(
            blob.content_hash,
            row.parser_name,
            row.profile,
        )
        payload = structured.model_dump(mode="json")
        result_uri = storage.put_object(
            result_key,
            io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            "application/json",
        )

        confidences = [
            value
            for value in (
                [block.confidence for block in structured.blocks]
                + [table.confidence for table in structured.tables]
            )
            if value is not None
        ]
        row.status = "completed"
        row.result_uri = result_uri
        row.summary = {
            "block_count": len(structured.blocks),
            "table_count": len(structured.tables),
            "warning_count": len(structured.warnings),
            "page_count": structured.document.page_count,
            "file_kind": classification.kind.value,
            "file_subtype": classification.subtype,
        }
        # MarkItDown / tables 等解析器 metadata 透出到 status 便于智能体判断
        for key in ("format", "char_count", "schema_version", "sheet_count"):
            if key in structured.metadata:
                row.summary[key] = structured.metadata[key]
        if structured.metadata.get("sheet_names") is not None:
            row.summary["sheet_names"] = structured.metadata.get("sheet_names")
        if legacy_result is not None:
            row.summary["legacy_converted"] = True
            row.summary["legacy_source_format"] = legacy_result.source_format
            row.summary["legacy_target_format"] = legacy_result.target_format
            row.summary["legacy_cache_hit"] = legacy_result.cache_hit
        row.overall_confidence = sum(confidences) / len(confidences) if confidences else None
        row.completed_at = datetime.now(timezone.utc)
        row.error = None
        from app.core.metrics import inc_metric

        inc_metric("parse_completed_total", profile=row.profile)
    except Exception as exc:  # pragma: no cover - exercised via failed parse tests
        row.status = "failed"
        row.error = str(exc)[:1000]
        row.completed_at = datetime.now(timezone.utc)
        from app.core.metrics import inc_metric

        inc_metric("parse_failed_total", profile=row.profile)
        # Map common dependency failures into summary for callers
        err_l = str(exc).lower()
        if any(tok in err_l for tok in ("timeout", "timed out")):
            row.summary = {**(row.summary or {}), "message_code": "parse_timeout"}
        elif any(tok in err_l for tok in ("litellm", "mineru", "connection", "503", "502")):
            row.summary = {**(row.summary or {}), "message_code": "parse_dependency_error"}
    db.flush()
    return row


def load_structured_content(storage: StorageBackend, parse_result: ParseResult) -> StructuredContent:
    if not parse_result.result_uri:
        raise ValueError("Parse result has no content URI")
    # memory://, local://, s3://, file:// — same key extraction as GC/lifecycle
    key = storage_key_from_uri(parse_result.result_uri)
    raw = storage.get_object(key)
    return StructuredContent.model_validate_json(raw)


def filter_structured_content(
    content: StructuredContent,
    *,
    pages: str | None = None,
    types: str | None = None,
    include_confidence: bool = True,
    smart_mode: bool = True,
    mime_type: str | None = None,
    filename: str | None = None,
    mode: str = "full",
    max_chars: int | None = None,
    offset: int = 0,
    sheets: str | None = None,
) -> StructuredContent:
    from app.parsers.content_view import apply_content_view
    from app.parsers.smart_markdown import apply_smart_mode_to_content

    page_set = _parse_page_range(pages, content.document.page_count)
    type_set = {item.strip() for item in types.split(",") if item.strip()} if types else None

    blocks = content.blocks
    tables = content.tables
    if page_set is not None:
        blocks = [block for block in blocks if block.page in page_set]
        tables = [table for table in tables if table.page in page_set]
    if type_set is not None:
        blocks = [block for block in blocks if block.type in type_set]
        if "table" not in type_set:
            tables = []

    if not include_confidence:
        blocks = [
            block.model_copy(update={"confidence": None})
            for block in blocks
        ]
        tables = [
            table.model_copy(update={"confidence": None, "cell_confidences": None})
            for table in tables
        ]

    filtered = content.model_copy(update={"blocks": blocks, "tables": tables})

    # 优先使用调用方传入的 mime/filename（引用层更可靠），便于识别电子表格
    if mime_type or filename:
        meta = dict(filtered.metadata or {})
        if mime_type:
            meta["mime_type"] = mime_type
        if filename:
            meta["filename"] = filename
        filtered = filtered.model_copy(update={"metadata": meta})

    filtered = apply_smart_mode_to_content(filtered, smart_mode=smart_mode)
    return apply_content_view(
        filtered,
        mode=mode,
        max_chars=max_chars,
        offset=offset,
        sheets=sheets,
        mime_type=mime_type,
        filename=filename,
    )


def _parse_page_range(raw: str | None, page_count: int | None) -> set[int] | None:
    if not raw:
        return None
    pages: set[int] = set()
    try:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start = int(start_text)
                end = int(end_text)
                if start > end:  # B5: catch reversed ranges like "5-3" instead of silently returning empty
                    raise ValueError(f"Page range start ({start}) must be <= end ({end})")
                pages.update(range(start, end + 1))
            else:
                pages.add(int(part))
    except ValueError as exc:
        raise ValueError(f"Invalid pages parameter: {raw!r}") from exc
    if page_count is not None:
        pages = {page for page in pages if 1 <= page <= page_count}
    return pages


def _filename_from_metadata(db: Session, content_hash: str) -> str | None:
    metadata_row = db.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == content_hash,
            ParseResult.profile == "metadata",
        )
    )
    if metadata_row and metadata_row.summary:
        filename = metadata_row.summary.get("filename")
        return str(filename) if filename else None
    return None


def _filename_from_reference(db: Session, content_hash: str) -> str | None:
    from app.models.file_reference import FileReference

    ref = db.scalar(
        select(FileReference)
        .where(FileReference.content_hash == content_hash)
        .limit(1)
    )
    if ref and ref.original_filename:
        return str(ref.original_filename)
    return None


def _suffix_for_blob(mime_type: str | None, *, filename_hint: str | None) -> str:
    if filename_hint:
        suffix = Path(filename_hint).suffix
        if suffix:
            return suffix
    if mime_type and mime_type in MIME_TO_SUFFIX:
        return MIME_TO_SUFFIX[mime_type]
    return ".bin"


def get_profile_parse_status(
    db: Session,
    content_hash: str,
    *,
    filename: str = "",
    mime_type: str = "application/octet-stream",
    file_data: BinaryIO | bytes = b"",
    profile: str = ProcessingLevel.LAYOUT.value,
) -> str:
    try:
        parser, _ = resolve_parser_for_profile(
            db,
            content_hash=content_hash,
            filename=filename,
            mime_type=mime_type,
            file_data=file_data,
            profile=profile,
        )
    except ValueError:
        return "not_started"
    row = db.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == content_hash,
            ParseResult.parser_name == parser.name,
            ParseResult.parser_version == parser.version,
            ParseResult.profile == profile,
        )
    )
    return row.status if row else "not_started"


def get_layout_parse_status(
    db: Session,
    content_hash: str,
    *,
    filename: str = "",
    mime_type: str = "application/octet-stream",
    file_data: BinaryIO | bytes = b"",
) -> str:
    return get_profile_parse_status(
        db,
        content_hash,
        filename=filename,
        mime_type=mime_type,
        file_data=file_data,
        profile=ProcessingLevel.LAYOUT.value,
    )


def execute_layout_parse(db: Session, parse_result_id: uuid.UUID, **kwargs) -> ParseResult:
    return execute_parse(db, parse_result_id, **kwargs)


def cache_key_fields(settings: Settings | None = None) -> tuple[str, str, str]:
    """Backward-compatible helper for routes/tests."""
    settings = settings or get_settings()
    return settings.layout_parser_name, settings.layout_parser_version, ProcessingLevel.LAYOUT.value