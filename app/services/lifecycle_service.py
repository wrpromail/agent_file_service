from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.security import get_owned_reference
from app.core.storage import GC_PURGED_URI, StorageBackend, blob_storage_key, storage_key_from_uri
from app.models.blob import Blob
from app.models.blob import GC_STATE_IDLE, GC_STATE_PURGING
from app.models.file_reference import FileReference
from app.models.parse_result import ParseResult
from app.schemas.files import FileReferenceSummary, SessionFilesResponse


def gc_blob(db: Session, storage: StorageBackend, content_hash: str) -> bool:
    """Claim a zero-ref blob for GC, delete its storage + parse results, then settle.

    Race-safe against concurrent uploads: the blob is atomically flipped from
    ``idle`` to ``purging`` only if ``ref_count == 0``. While ``purging``,
    ``store_or_reuse_blob`` will not reuse the row's storage. After cleanup we
    re-check ``ref_count``: if a concurrent upload incremented it during the
    purge window, the row is preserved with ``gc_state`` reset to ``idle`` (its
    storage may have been purged; the uploader will re-create storage via the
    ``GC_PURGED_URI`` path in ``store_or_reuse_blob``). Otherwise the blob row is
    deleted entirely.
    """
    blob = db.get(Blob, content_hash)
    if blob is None:
        return False
    if blob.ref_count > 0:
        return False
    if blob.gc_state == GC_STATE_PURGING:
        # Already being purged by another worker; let that worker finish.
        return False

    claimed = db.execute(
        update(Blob)
        .where(
            Blob.content_hash == content_hash,
            Blob.ref_count == 0,
            Blob.gc_state == GC_STATE_IDLE,
        )
        .values(gc_state=GC_STATE_PURGING)
    )
    if claimed.rowcount == 0:
        # Someone changed ref_count or gc_state between our read and claim.
        return False
    db.flush()

    parse_rows = db.scalars(
        select(ParseResult).where(ParseResult.content_hash == content_hash)
    ).all()
    for row in parse_rows:
        if row.result_uri:
            storage.delete_object(storage_key_from_uri(row.result_uri))

    # 派生产物（如 legacy Office 规范化 OOXML）随原 blob 一并清理
    from app.parsers.legacy_office import purge_legacy_derivatives

    purge_legacy_derivatives(storage, content_hash)
    _purge_custom_parse_for_hash(db, storage, content_hash)
    _purge_mineru_workdir(content_hash)

    if blob.storage_uri != GC_PURGED_URI:
        storage.delete_object(blob_storage_key(content_hash))

    db.execute(delete(ParseResult).where(ParseResult.content_hash == content_hash))

    remaining_refs = db.scalar(
        select(func.count())
        .select_from(FileReference)
        .where(
            FileReference.content_hash == content_hash,
            FileReference.deleted_at.is_(None),  # B1: only count active (non-deleted) references
        )
    )
    if remaining_refs == 0:
        # No active references: leave the blob row as a tombstone (gc://purged).
        # This allows in-flight race detection without needing to delete and re-create the row.
        # Note: we do NOT hard-delete FileReference records here; soft deletion preserves audit history.
        blob.storage_uri = GC_PURGED_URI
        blob.gc_state = GC_STATE_IDLE
    else:
        # A reference appeared during the purge window (e.g. a concurrent
        # upload). Preserve the row, mark storage as purged, and release GC
        # ownership. The next uploader hitting this hash will see the purged
        # URI and re-upload the blob bytes.
        blob.storage_uri = GC_PURGED_URI
        blob.gc_state = GC_STATE_IDLE
    db.flush()
    return True



def _soft_delete_reference(
    db: Session,
    storage: StorageBackend,
    reference: FileReference,
) -> None:
    if reference.deleted_at is not None:
        return

    reference.deleted_at = datetime.now(timezone.utc)
    blob = db.get(Blob, reference.content_hash)
    if blob is None:
        db.flush()
        return

    # Decrement ref_count atomically. ``with_for_update`` locks the row on
    # Postgres so the decrement is serialised against GC's conditional claim;
    # SQLite ignores the lock but the conditional UPDATE still keeps the value
    # consistent within this session's view.
    blob = db.scalar(
        select(Blob).where(Blob.content_hash == reference.content_hash).with_for_update()
    )
    if blob is None:
        db.flush()
        return

    if blob.ref_count > 0:
        blob.ref_count -= 1
    db.flush()
    if blob.ref_count == 0 and blob.gc_state == GC_STATE_IDLE:
        gc_blob(db, storage, blob.content_hash)


def delete_reference(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    reference_id: uuid.UUID,
) -> None:
    reference = get_owned_reference(db, tenant_id, reference_id)
    _soft_delete_reference(db, storage, reference)


def expire_stale_references(
    db: Session,
    storage: StorageBackend,
    *,
    now: datetime | None = None,
) -> int:
    from app.config import get_settings

    now = now or datetime.now(timezone.utc)
    batch = max(1, get_settings().gc_batch_size)
    references = db.scalars(
        select(FileReference)
        .where(
            FileReference.deleted_at.is_(None),
            FileReference.expires_at.is_not(None),
            FileReference.expires_at < now,
        )
        .limit(batch)
    ).all()
    for reference in references:
        _soft_delete_reference(db, storage, reference)
    return len(references)


def reclaim_stuck_work(db: Session, storage: StorageBackend) -> dict[str, int]:
    """Fail stuck processing jobs and finish or release stuck purging blobs."""
    from datetime import timedelta

    from app.config import get_settings
    from app.models.custom_parse_job import CustomParseJob

    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max(60, settings.gc_processing_reclaim_seconds))

    parse_failed = 0
    stuck_parses = db.scalars(
        select(ParseResult).where(
            ParseResult.status == "processing",
            # B2: use processing_started_at (when worker actually claimed the task).
            # Fall back to created_at for rows written before this field was added (NULL).
            (ParseResult.processing_started_at < cutoff)
            | ((ParseResult.processing_started_at == None) & (ParseResult.created_at < cutoff)),  # noqa: E711
        )
    ).all()
    for row in stuck_parses:
        row.status = "failed"
        row.error = (row.error or "Reclaimed: stuck in processing past timeout")[:1000]
        row.completed_at = now
        parse_failed += 1

    custom_failed = 0
    stuck_customs = db.scalars(
        select(CustomParseJob).where(
            CustomParseJob.status == "processing",
            # B2: use processing_started_at; fall back to created_at for old rows
            ((CustomParseJob.processing_started_at < cutoff) |
             ((CustomParseJob.processing_started_at == None) & (CustomParseJob.created_at < cutoff))),  # noqa: E711
        )
    ).all()
    for job in stuck_customs:
        job.status = "failed"
        job.error = (job.error or "Reclaimed: stuck in processing past timeout")[:1000]
        job.completed_at = now
        custom_failed += 1

    purging_reclaimed = 0
    stuck_purging = db.scalars(
        select(Blob).where(Blob.gc_state == GC_STATE_PURGING)
    ).all()
    for blob in stuck_purging:
        # Crashed mid-purge: finish cleanup or release ownership.
        if blob.ref_count > 0:
            if blob.storage_uri != GC_PURGED_URI and not storage.exists(
                blob_storage_key(blob.content_hash)
            ):
                blob.storage_uri = GC_PURGED_URI
            blob.gc_state = GC_STATE_IDLE
            purging_reclaimed += 1
            continue
        # ref_count == 0: complete purge under claim (already purging).
        parse_rows = db.scalars(
            select(ParseResult).where(ParseResult.content_hash == blob.content_hash)
        ).all()
        for row in parse_rows:
            if row.result_uri:
                storage.delete_object(storage_key_from_uri(row.result_uri))
        from app.parsers.legacy_office import purge_legacy_derivatives

        purge_legacy_derivatives(storage, blob.content_hash)
        _purge_custom_parse_for_hash(db, storage, blob.content_hash)
        _purge_mineru_workdir(blob.content_hash)
        if blob.storage_uri != GC_PURGED_URI:
            storage.delete_object(blob_storage_key(blob.content_hash))
        db.execute(delete(ParseResult).where(ParseResult.content_hash == blob.content_hash))
        remaining_refs = db.scalar(
            select(func.count())
            .select_from(FileReference)
            .where(
                FileReference.content_hash == blob.content_hash,
                FileReference.deleted_at.is_(None),  # B1: only count active (non-deleted) references
            )
        )
        if remaining_refs == 0:
            blob.storage_uri = GC_PURGED_URI
            blob.gc_state = GC_STATE_IDLE
        else:
            blob.storage_uri = GC_PURGED_URI
            blob.gc_state = GC_STATE_IDLE
        purging_reclaimed += 1

    db.flush()
    return {
        "parse_reclaimed": parse_failed,
        "custom_parse_reclaimed": custom_failed,
        "purging_reclaimed": purging_reclaimed,
    }


def _purge_custom_parse_for_hash(db: Session, storage: StorageBackend, content_hash: str) -> None:
    from app.models.custom_parse_job import CustomParseJob

    jobs = db.scalars(
        select(CustomParseJob).where(CustomParseJob.content_hash == content_hash)
    ).all()
    for job in jobs:
        if job.result_uri:
            storage.delete_object(storage_key_from_uri(job.result_uri))
        db.delete(job)


def _purge_mineru_workdir(content_hash: str) -> None:
    import shutil
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    work = Path(settings.mineru_work_dir) / content_hash[:16]
    if work.is_dir():
        shutil.rmtree(work, ignore_errors=True)


def run_blob_gc(db: Session, storage: StorageBackend) -> int:
    """Sweep blobs with ``ref_count == 0``. Race-safe: ``gc_blob`` re-checks
    and atomically claims each blob, so concurrent workers / uploads cannot
    double-purge or reuse purged storage."""
    from app.config import get_settings

    reclaim_stuck_work(db, storage)
    settings = get_settings()
    batch = max(1, settings.gc_batch_size)
    blobs = db.scalars(
        select(Blob)
        .where(Blob.ref_count <= 0, Blob.gc_state == GC_STATE_IDLE)
        .limit(batch)
    ).all()
    count = 0
    for blob in blobs:
        if gc_blob(db, storage, blob.content_hash):
            count += 1
    return count


def list_session_files(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    limit: int = 20,
    offset: int = 0,
) -> SessionFilesResponse:
    from app.services.file_service import (
        _build_file_metadata,
        _metadata_from_summary,
        _metadata_map_for_hashes,
    )

    # B4: enforce pagination to prevent OOM on sessions with many files
    limit = min(max(1, limit), 200)
    offset = max(0, offset)

    references = db.scalars(
        select(FileReference)
        .where(
            FileReference.tenant_id == tenant_id,
            FileReference.session_id == session_id,
            FileReference.deleted_at.is_(None),
        )
        .order_by(FileReference.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    meta_by_hash = _metadata_map_for_hashes(
        db, [reference.content_hash for reference in references]
    )

    files: list[FileReferenceSummary] = []
    for reference in references:
        metadata_row = meta_by_hash.get(reference.content_hash)
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
        files.append(
            FileReferenceSummary(
                reference_id=reference.id,
                content_hash=reference.content_hash,
                original_filename=reference.original_filename,
                metadata=file_metadata,
                created_at=reference.created_at,
                expires_at=reference.expires_at,
            )
        )

    return SessionFilesResponse(session_id=session_id, files=files)

