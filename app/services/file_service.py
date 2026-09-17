import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.hashing import UploadTooLargeError, read_and_hash
from app.core.storage import StorageBackend, blob_storage_key
from app.models.file_reference import FileReference
from app.models.parse_result import ParseResult
from app.parsers.registry import get_registry
from app.parsers.types import FileKind, ProcessingLevel
from app.core.metrics import inc_metric
from app.schemas.files import (
    ArchiveChildSummary,
    ArchiveExpandInfo,
    ArchiveMemberItem,
    ArchiveMembersResponse,
    CloneRequest,
    CloneResponse,
    FileDetailResponse,
    FileListItem,
    FileListResponse,
    FileMetadata,
    FileStatusResponse,
    ParseResultSummary,
    ReferenceUpdateRequest,
    UploadResponse,
)
from app.services.quota_service import QuotaExceededError, check_upload_quota, raise_http_quota
from app.services.archive_service import (
    ArchiveError,
    archive_summary_dict,
    extract_members_batch,
    inspect_archive,
    is_archive_filename,
)
from app.services.auto_parse_policy import decide_auto_parse, reason_message
from app.services.dedup import BlobGcConflictError, store_or_reuse_blob
from app.services.parse_service import schedule_profile_parse


def _upsert_metadata_parse_result(
    db: Session,
    content_hash: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    extra: dict,
) -> None:
    summary = {
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        **extra,
    }
    existing = db.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == content_hash,
            ParseResult.profile == "metadata",
        )
    )
    if existing is None:
        db.add(
            ParseResult(
                content_hash=content_hash,
                parser_name="metadata",
                parser_version="0.1.0",
                profile="metadata",
                status="completed",
                summary=summary,
            )
        )
    else:
        existing.summary = summary
        existing.status = "completed"
    db.flush()


def _metadata_from_summary(summary: dict, filename: str, mime_type: str, size_bytes: int) -> FileMetadata:
    return FileMetadata(
        filename=summary.get("filename", filename),
        mime_type=summary.get("mime_type", mime_type),
        size_bytes=summary.get("size_bytes", size_bytes),
        page_count=summary.get("page_count"),
        sheet_count=summary.get("sheet_count"),
        extra={
            k: v
            for k, v in summary.items()
            if k not in {"filename", "mime_type", "size_bytes", "page_count", "sheet_count"}
        },
    )


def _build_file_metadata(filename: str, mime_type: str, size_bytes: int, extra: dict) -> FileMetadata:
    return FileMetadata(
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        page_count=extra.get("page_count"),
        sheet_count=extra.get("sheet_count"),
        extra={k: v for k, v in extra.items() if k not in {"page_count", "sheet_count"}},
    )


def parse_custom_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("metadata must be a JSON object")
    return data


# Backward-compatible alias for older imports / tests.
_parse_custom_metadata = parse_custom_metadata


@dataclass
class IngestResult:
    reference: FileReference
    deduplicated: bool
    file_metadata: FileMetadata
    parse_status: str
    auto_parse_applied: bool
    auto_parse_skip_reason: str | None
    auto_parse_message: str | None


def _rewind(file_data: BinaryIO) -> None:
    if hasattr(file_data, "seek"):
        file_data.seek(0)


def _ingest_file(
    db: Session,
    storage: StorageBackend,
    *,
    file_data: BinaryIO,
    content_hash: str,
    tenant_id: str,
    filename: str,
    content_type: str | None,
    session_id: str | None,
    agent_id: str | None,
    uploaded_by: str | None,
    custom_metadata: dict[str, Any],
    auto_parse: bool,
    expires_at: datetime | None,
    background_tasks: BackgroundTasks | None,
    metadata_extra_overlay: dict[str, Any] | None = None,
) -> IngestResult:
    """Store blob + reference + metadata + optional auto_parse from a seekable stream."""
    registry = get_registry()
    _rewind(file_data)
    metadata_result = registry.get_metadata_extractor().extract(
        file_data,
        filename,
        content_type or "",
    )
    extra = dict(metadata_result.extra or {})
    if metadata_extra_overlay:
        extra.update(metadata_extra_overlay)

    _rewind(file_data)
    _blob, deduplicated = store_or_reuse_blob(
        db,
        storage,
        content_hash=content_hash,
        mime_type=metadata_result.mime_type,
        size_bytes=metadata_result.size_bytes,
        file_buffer=file_data,
    )

    reference = FileReference(
        content_hash=content_hash,
        tenant_id=tenant_id,
        session_id=session_id,
        agent_id=agent_id,
        original_filename=filename,
        uploaded_by=uploaded_by,
        custom_metadata=custom_metadata,
        expires_at=expires_at,
    )
    db.add(reference)
    db.flush()

    _upsert_metadata_parse_result(
        db,
        content_hash,
        filename,
        metadata_result.mime_type,
        metadata_result.size_bytes,
        extra,
    )

    decision = decide_auto_parse(
        auto_parse_requested=auto_parse,
        file_kind=str(extra.get("file_kind") or "unknown"),
        size_bytes=metadata_result.size_bytes,
        page_count=extra.get("page_count"),
        sheet_count=extra.get("sheet_count"),
    )

    parse_status = "not_started"
    if decision.should_parse:
        _rewind(file_data)
        row = schedule_profile_parse(
            db,
            content_hash,
            filename=filename,
            mime_type=metadata_result.mime_type,
            file_data=file_data,
            profile=decision.profile,
            background_tasks=background_tasks,
        )
        parse_status = row.status
        # Spreadsheet: also materialize structured tables JSON for agents/codegen.
        settings = get_settings()
        kind = str(extra.get("file_kind") or "").lower()
        if (
            kind == "spreadsheet"
            and getattr(settings, "auto_parse_spreadsheet_also_tables", True)
            and decision.profile != ProcessingLevel.TABLES.value
        ):
            try:
                _rewind(file_data)
                schedule_profile_parse(
                    db,
                    content_hash,
                    filename=filename,
                    mime_type=metadata_result.mime_type,
                    file_data=file_data,
                    profile=ProcessingLevel.TABLES.value,
                    background_tasks=background_tasks,
                )
            except Exception:
                # layout still primary; tables is best-effort on auto path
                pass

    file_metadata = _build_file_metadata(
        filename,
        metadata_result.mime_type,
        metadata_result.size_bytes,
        extra,
    )
    return IngestResult(
        reference=reference,
        deduplicated=deduplicated,
        file_metadata=file_metadata,
        parse_status=parse_status,
        auto_parse_applied=decision.should_parse,
        auto_parse_skip_reason=None if decision.should_parse else decision.reason,
        auto_parse_message=reason_message(decision.reason),
    )




def _expand_archive_children(
    db: Session,
    storage: StorageBackend,
    *,
    parent_file: BinaryIO,
    parent_filename: str,
    parent_mime: str,
    parent_reference_id,
    tenant_id: str,
    session_id: str | None,
    agent_id: str | None,
    uploaded_by: str | None,
    auto_parse: bool,
    expires_at: datetime | None,
    background_tasks: BackgroundTasks | None,
    password: str | None,
    inspect_result,
) -> list[ArchiveChildSummary]:
    from hashlib import sha256
    from io import BytesIO

    settings = get_settings()
    children: list[ArchiveChildSummary] = []
    parent_meta_base = {
        "extracted_from": str(parent_reference_id),
    }

    paths = [m.path for m in inspect_result.eligible]
    members = extract_members_batch(
        parent_file,
        filename=parent_filename,
        member_paths=paths,
        password=password,
        mime_type=parent_mime,
        max_member_bytes=settings.archive_max_member_bytes,
    )

    for member in inspect_result.eligible:
        member_bytes = members[member.path]
        child_hash = sha256(member_bytes).hexdigest()
        child_meta = {
            **parent_meta_base,
            "archive_path": member.path,
        }
        ingested = _ingest_file(
            db,
            storage,
            file_data=BytesIO(member_bytes),
            content_hash=child_hash,
            tenant_id=tenant_id,
            filename=member.path,
            content_type=None,
            session_id=session_id,
            agent_id=agent_id,
            uploaded_by=uploaded_by,
            custom_metadata=child_meta,
            auto_parse=auto_parse,
            expires_at=expires_at,
            background_tasks=background_tasks,
        )
        children.append(
            ArchiveChildSummary(
                reference_id=ingested.reference.id,
                filename=member.path,
                content_hash=child_hash,
                deduplicated=ingested.deduplicated,
                parse_status=ingested.parse_status,
                metadata=ingested.file_metadata,
            )
        )
    return children


def upload_file(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    file_obj,
    filename: str,
    content_type: str | None,
    session_id: str | None,
    agent_id: str | None,
    uploaded_by: str | None,
    custom_metadata: dict[str, Any],
    auto_parse: bool,
    expires_at: datetime | None = None,
    background_tasks: BackgroundTasks | None = None,
    archive_password: str | None = None,
    expand_archives: bool = True,
) -> UploadResponse:
    settings = get_settings()
    try:
        spooled, content_hash, size_bytes = read_and_hash(
            file_obj,
            max_bytes=settings.max_upload_bytes,
        )
    except UploadTooLargeError as exc:
        max_mib = exc.max_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413),
            detail=(
                f"文件大小超过上传上限（max_upload_bytes={exc.max_bytes}，"
                f"约 {max_mib:.0f} MiB）。请压缩后重试，或调整 config.yaml 中的 max_upload_bytes。"
            ),
        ) from exc
    try:
        try:
            check_upload_quota(db, tenant_id=tenant_id, incoming_bytes=size_bytes, settings=settings)
        except QuotaExceededError as exc:
            raise_http_quota(exc)
        try:
            ingested = _ingest_file(
                db,
                storage,
                file_data=spooled,
                content_hash=content_hash,
                tenant_id=tenant_id,
                filename=filename,
                content_type=content_type,
                session_id=session_id,
                agent_id=agent_id,
                uploaded_by=uploaded_by,
                custom_metadata=custom_metadata,
                auto_parse=auto_parse,
                expires_at=expires_at,
                background_tasks=background_tasks,
            )
        except BlobGcConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

        reference = ingested.reference
        deduplicated = ingested.deduplicated
        file_metadata = ingested.file_metadata
        parse_status = ingested.parse_status
        auto_applied = ingested.auto_parse_applied
        auto_skip = ingested.auto_parse_skip_reason
        auto_msg = ingested.auto_parse_message

        archive_info: ArchiveExpandInfo | None = None
        file_kind = str((file_metadata.extra or {}).get("file_kind") or "")
        should_expand = (
            expand_archives
            and settings.archive_expand_enabled
            and (
                file_kind == FileKind.ARCHIVE.value
                or is_archive_filename(filename, content_type or "")
            )
        )

        if should_expand:
            try:
                _rewind(spooled)
                inspect_result = inspect_archive(
                    spooled,
                    filename=filename,
                    mime_type=file_metadata.mime_type or content_type or "",
                    password=archive_password or None,
                    settings=settings,
                )
            except ArchiveError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=exc.message,
                ) from exc

            summary = archive_summary_dict(inspect_result)
            # 写回父包 metadata summary
            extra = dict(file_metadata.extra or {})
            extra["archive"] = summary
            file_metadata = _build_file_metadata(
                filename,
                file_metadata.mime_type,
                file_metadata.size_bytes,
                extra,
            )
            _upsert_metadata_parse_result(
                db,
                content_hash,
                filename,
                file_metadata.mime_type,
                file_metadata.size_bytes,
                extra,
            )

            children: list[ArchiveChildSummary] = []
            if inspect_result.status == "expanded" and inspect_result.eligible:
                try:
                    children = _expand_archive_children(
                        db,
                        storage,
                        parent_file=spooled,
                        parent_filename=filename,
                        parent_mime=file_metadata.mime_type or "",
                        parent_reference_id=reference.id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        uploaded_by=uploaded_by,
                        auto_parse=auto_parse,
                        expires_at=expires_at,
                        background_tasks=background_tasks,
                        password=archive_password or None,
                        inspect_result=inspect_result,
                    )
                except ArchiveError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=exc.message,
                    ) from exc

            archive_info = ArchiveExpandInfo(
                status=inspect_result.status,
                format=inspect_result.format,
                eligible_count=inspect_result.eligible_count,
                skipped_count=inspect_result.skipped_count,
                max_expand_files=inspect_result.max_expand_files,
                message=inspect_result.message,
                encrypted=inspect_result.encrypted,
                children=children,
            )

        inc_metric("uploads_total")
        inc_metric("upload_bytes_total", float(size_bytes))
        if deduplicated:
            inc_metric("dedup_hits_total")

        return UploadResponse(
            reference_id=reference.id,
            content_hash=content_hash,
            deduplicated=deduplicated,
            metadata=file_metadata,
            parse_status=parse_status,
            expires_at=reference.expires_at,
            auto_parse_requested=auto_parse,
            auto_parse_applied=auto_applied,
            auto_parse_skip_reason=auto_skip,
            auto_parse_message=auto_msg,
            archive=archive_info,
        )
    finally:
        spooled.close()


def get_parse_summaries(db: Session, content_hash: str) -> list[ParseResultSummary]:
    rows = db.scalars(select(ParseResult).where(ParseResult.content_hash == content_hash)).all()
    return [
        ParseResultSummary(
            profile=row.profile,
            status=row.status,
            parser_name=row.parser_name,
            overall_confidence=row.overall_confidence,
        )
        for row in rows
    ]


def _metadata_map_for_hashes(db: Session, hashes: list[str]) -> dict[str, ParseResult]:
    if not hashes:
        return {}
    rows = db.scalars(
        select(ParseResult).where(
            ParseResult.content_hash.in_(hashes),
            ParseResult.profile == "metadata",
        )
    ).all()
    return {row.content_hash: row for row in rows}


def get_file_detail(db: Session, reference: FileReference) -> FileDetailResponse:
    metadata_row = db.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == reference.content_hash,
            ParseResult.profile == "metadata",
        )
    )
    if metadata_row and metadata_row.summary:
        file_metadata = _metadata_from_summary(
            metadata_row.summary,
            reference.original_filename,
            reference.blob.mime_type,
            reference.blob.size_bytes,
        )
    else:
        file_metadata = _build_file_metadata(
            reference.original_filename,
            reference.blob.mime_type,
            reference.blob.size_bytes,
            {},
        )
    return FileDetailResponse(
        reference_id=reference.id,
        content_hash=reference.content_hash,
        tenant_id=reference.tenant_id,
        session_id=reference.session_id,
        agent_id=reference.agent_id,
        expires_at=reference.expires_at,
        metadata=file_metadata,
        custom_metadata=reference.custom_metadata,
        parse_results=get_parse_summaries(db, reference.content_hash),
    )


def get_file_status(db: Session, reference: FileReference, profile: str | None) -> FileStatusResponse:
    detail = get_file_detail(db, reference)
    parse_results = detail.parse_results
    if profile:
        parse_results = [item for item in parse_results if item.profile == profile]
    return FileStatusResponse(
        reference_id=detail.reference_id,
        metadata=detail.metadata,
        parse_results=parse_results,
    )


def update_reference(
    db: Session,
    reference: FileReference,
    body: ReferenceUpdateRequest,
    *,
    fields_set: set[str] | None = None,
) -> FileDetailResponse:
    """Update mutable FileReference fields. Does not touch blob or parse results."""
    set_fields = fields_set if fields_set is not None else set(body.model_fields_set)

    if "session_id" in set_fields:
        reference.session_id = body.session_id
    if "agent_id" in set_fields:
        reference.agent_id = body.agent_id
    if "uploaded_by" in set_fields:
        reference.uploaded_by = body.uploaded_by
    if "expires_at" in set_fields:
        reference.expires_at = body.expires_at
    if "custom_metadata" in set_fields and body.custom_metadata is not None:
        mode = (body.metadata_mode or "merge").strip().lower()
        if mode == "replace":
            reference.custom_metadata = dict(body.custom_metadata)
        else:
            merged = dict(reference.custom_metadata or {})
            merged.update(body.custom_metadata)
            reference.custom_metadata = merged
    db.flush()
    return get_file_detail(db, reference)


def list_archive_members(
    db: Session,
    storage: StorageBackend,
    reference: FileReference,
    *,
    password: str | None = None,
) -> ArchiveMembersResponse:
    """List eligible/skipped members of a zip/tar parent package."""
    filename = reference.original_filename
    mime = reference.blob.mime_type or ""
    if not is_archive_filename(filename, mime):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is not a supported archive format",
        )

    settings = get_settings()
    key = blob_storage_key(reference.content_hash)
    # Stream archive to a seekable spool for inspect
    import tempfile

    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        for chunk in storage.iter_object(key):
            spool.write(chunk)
        spool.seek(0)
        try:
            inspect_result = inspect_archive(
                spool,
                filename=filename,
                mime_type=mime,
                password=password,
                settings=settings,
            )
        except ArchiveError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    finally:
        spool.close()

    members: list[ArchiveMemberItem] = []
    for m in inspect_result.eligible:
        members.append(ArchiveMemberItem(path=m.path, size=m.size, eligible=True))
    for skip in inspect_result.skipped:
        members.append(
            ArchiveMemberItem(
                path=skip.get("path", ""),
                size=0,
                eligible=False,
                skip_reason=skip.get("reason"),
            )
        )
    return ArchiveMembersResponse(
        reference_id=reference.id,
        format=inspect_result.format,
        status=inspect_result.status,
        encrypted=inspect_result.encrypted,
        max_expand_files=inspect_result.max_expand_files,
        eligible_count=inspect_result.eligible_count,
        skipped_count=inspect_result.skipped_count,
        message=inspect_result.message or None,
        members=members,
    )


def download_reference_bytes(
    storage: StorageBackend,
    reference: FileReference,
) -> tuple[bytes, str, str]:
    """Return raw blob bytes, filename, and mime type for a tenant-owned reference."""
    key = blob_storage_key(reference.content_hash)
    data = storage.get_object(key)
    return data, reference.original_filename, reference.blob.mime_type


def download_reference_stream(
    storage: StorageBackend,
    reference: FileReference,
    *,
    chunk_size: int = 1024 * 1024,
):
    """Yield blob bytes in chunks for StreamingResponse."""
    key = blob_storage_key(reference.content_hash)
    yield from storage.iter_object(key, chunk_size=chunk_size)


# ── Feature 1: paginated file list ───────────────────────────────────────────

def list_files(
    db: Session,
    *,
    tenant_id: str,
    session_id: str | None = None,
    agent_id: str | None = None,
    uploaded_by: str | None = None,
    filename: str | None = None,
    content_hash: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> FileListResponse:
    """Return a paginated, filtered list of FileReferences for *tenant_id*.

    All filter parameters are optional; omitting them returns all live
    (deleted_at IS NULL) references for the tenant, ordered by created_at DESC.
    """
    from sqlalchemy import func as sa_func

    limit = min(limit, 100)  # hard cap

    base_conditions = [
        FileReference.tenant_id == tenant_id,
        FileReference.deleted_at.is_(None),
    ]
    if session_id is not None:
        base_conditions.append(FileReference.session_id == session_id)
    if agent_id is not None:
        base_conditions.append(FileReference.agent_id == agent_id)
    if uploaded_by is not None:
        base_conditions.append(FileReference.uploaded_by == uploaded_by)
    if filename is not None:
        base_conditions.append(FileReference.original_filename.ilike(f"%{filename}%"))
    if content_hash is not None:
        base_conditions.append(FileReference.content_hash == content_hash)

    total: int = db.scalar(
        select(sa_func.count())
        .select_from(FileReference)
        .where(*base_conditions)
    ) or 0

    references = db.scalars(
        select(FileReference)
        .where(*base_conditions)
        .order_by(FileReference.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    meta_by_hash = _metadata_map_for_hashes(
        db, [ref.content_hash for ref in references]
    )

    items: list[FileListItem] = []
    for ref in references:
        metadata_row = meta_by_hash.get(ref.content_hash)
        if metadata_row and metadata_row.summary:
            file_metadata = _metadata_from_summary(
                metadata_row.summary,
                ref.original_filename,
                ref.blob.mime_type,
                ref.blob.size_bytes,
            )
        else:
            file_metadata = _build_file_metadata(
                ref.original_filename,
                ref.blob.mime_type,
                ref.blob.size_bytes,
                {},
            )
        items.append(
            FileListItem(
                reference_id=ref.id,
                content_hash=ref.content_hash,
                original_filename=ref.original_filename,
                session_id=ref.session_id,
                agent_id=ref.agent_id,
                uploaded_by=ref.uploaded_by,
                custom_metadata=ref.custom_metadata,
                created_at=ref.created_at,
                expires_at=ref.expires_at,
                metadata=file_metadata,
            )
        )

    return FileListResponse(
        total=total,
        limit=limit,
        offset=offset,
        files=items,
    )


# ── Feature 2: reference clone ────────────────────────────────────────────────

def clone_reference(
    db: Session,
    *,
    source_reference: FileReference,
    body: CloneRequest,
    source_tenant_id: str,
) -> CloneResponse:
    """Create a new FileReference in *target_tenant_id* that points at the
    same Blob as *source_reference*, incrementing its ref_count atomically.

    The two references are fully independent after creation — each has its
    own lifecycle (expiry, deletion) and custom metadata.
    """
    from app.models.blob import GC_STATE_PURGING, Blob
    from app.core.storage import GC_PURGED_URI

    # Lock the Blob row so the ref_count increment is serialised against
    # concurrent GC decrements (same pattern as _soft_delete_reference).
    blob = db.scalar(
        select(Blob)
        .where(Blob.content_hash == source_reference.content_hash)
        .with_for_update()
    )
    if blob is None:
        from fastapi import HTTPException, status as http_status
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Blob row missing for the given reference",
        )

    if blob.gc_state == GC_STATE_PURGING or blob.storage_uri == GC_PURGED_URI:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "Blob storage is being garbage-collected or was purged; "
                "retry clone after the source file is re-uploaded."
            ),
        )

    blob.ref_count += 1

    # Build custom_metadata: auto fields first, caller-supplied fields override.
    auto_meta: dict[str, Any] = {
        "cloned_from": str(source_reference.id),
        "source_tenant_id": source_tenant_id,
    }
    merged_meta = {**auto_meta, **body.metadata}

    new_reference = FileReference(
        content_hash=source_reference.content_hash,
        tenant_id=body.target_tenant_id,
        session_id=body.session_id,
        agent_id=body.agent_id,
        original_filename=source_reference.original_filename,
        uploaded_by=None,
        custom_metadata=merged_meta,
        expires_at=body.expires_at,
    )
    db.add(new_reference)
    db.flush()  # ref_count increment + new reference committed together

    detail = get_file_detail(db, new_reference)

    return CloneResponse(
        reference_id=new_reference.id,
        source_reference_id=source_reference.id,
        content_hash=source_reference.content_hash,
        target_tenant_id=body.target_tenant_id,
        deduplicated=True,
        metadata=detail.metadata,
    )