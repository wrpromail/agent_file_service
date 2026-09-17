"""Race-safe blob store / reuse against concurrent GC."""
from __future__ import annotations

import time
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.storage import GC_PURGED_URI, StorageBackend, blob_storage_key
from app.models.blob import GC_STATE_IDLE, GC_STATE_PURGING, Blob

# How long an upload will wait for a concurrent GC purge to settle.
_GC_SETTLE_WAIT_SECONDS = 2.0
_GC_SETTLE_POLL_SECONDS = 0.05


class BlobGcConflictError(Exception):
    """Blob is still being purged after the settle wait; caller should retry."""

    def __init__(self, content_hash: str) -> None:
        self.content_hash = content_hash
        super().__init__(
            f"Blob {content_hash[:12]}… is still being garbage-collected; retry shortly."
        )


def get_blob_by_hash(db: Session, content_hash: str) -> Blob | None:
    return db.scalar(select(Blob).where(Blob.content_hash == content_hash))


def _poll_gc_settle(content_hash: str) -> str:
    """Poll GC state without holding locks on the caller's transaction.

    C2 fix: reuses a single Session across poll iterations (expire_all() flushes
    the identity map so each get() hits the database) instead of opening and
    closing a new connection every _GC_SETTLE_POLL_SECONDS — avoids exhausting
    the connection pool under concurrent uploads.

    Returns:
        ``"gone"`` — row deleted
        ``"idle"`` — row present and not purging
        ``"purging"`` — still purging after timeout
    """
    from app.db.session import SessionLocal

    deadline = time.monotonic() + _GC_SETTLE_WAIT_SECONDS
    poll_db = SessionLocal()
    try:
        while time.monotonic() < deadline:
            poll_db.expire_all()  # flush identity map; next get() hits DB
            blob = poll_db.get(Blob, content_hash)
            if blob is None:
                return "gone"
            if blob.gc_state != GC_STATE_PURGING:
                return "idle"
            time.sleep(_GC_SETTLE_POLL_SECONDS)

        # Final check after deadline
        poll_db.expire_all()
        blob = poll_db.get(Blob, content_hash)
        if blob is None:
            return "gone"
        if blob.gc_state != GC_STATE_PURGING:
            return "idle"
        return "purging"
    finally:
        poll_db.close()


def _rewind(file_buffer: BinaryIO) -> None:
    if hasattr(file_buffer, "seek"):
        file_buffer.seek(0)


def _reuse_locked_blob(
    db: Session,
    storage: StorageBackend,
    *,
    existing: Blob,
    content_hash: str,
    mime_type: str,
    file_buffer: BinaryIO,
) -> tuple[Blob, bool]:
    """Mutate a FOR UPDATE-locked idle (or purged-uri) blob for a new reference."""
    if existing.gc_state == GC_STATE_PURGING:
        raise BlobGcConflictError(content_hash)

    if existing.storage_uri == GC_PURGED_URI:
        key = blob_storage_key(content_hash)
        _rewind(file_buffer)
        storage_uri = storage.put_object(key, file_buffer, mime_type)
        existing.storage_uri = storage_uri
    elif existing.storage_uri != GC_PURGED_URI:
        # Live URI: refuse reuse if physical object vanished under us.
        key = blob_storage_key(content_hash)
        if not storage.exists(key):
            _rewind(file_buffer)
            existing.storage_uri = storage.put_object(key, file_buffer, mime_type)

    existing.ref_count += 1
    existing.gc_state = GC_STATE_IDLE
    db.flush()
    return existing, True


def _create_blob(
    db: Session,
    storage: StorageBackend,
    *,
    content_hash: str,
    mime_type: str,
    size_bytes: int,
    file_buffer: BinaryIO,
) -> tuple[Blob, bool]:
    key = blob_storage_key(content_hash)
    _rewind(file_buffer)
    storage_uri = storage.put_object(key, file_buffer, mime_type)
    blob = Blob(
        content_hash=content_hash,
        size_bytes=size_bytes,
        mime_type=mime_type,
        storage_uri=storage_uri,
        ref_count=1,
        gc_state=GC_STATE_IDLE,
    )
    try:
        with db.begin_nested():
            db.add(blob)
            db.flush()
        return blob, False
    except IntegrityError:
        # Concurrent creator won the PK; content-addressed object is fine to keep.
        existing = db.scalar(
            select(Blob).where(Blob.content_hash == content_hash).with_for_update()
        )
        if existing is None:
            raise
        if existing.gc_state == GC_STATE_PURGING:
            raise BlobGcConflictError(content_hash) from None
        return _reuse_locked_blob(
            db,
            storage,
            existing=existing,
            content_hash=content_hash,
            mime_type=mime_type,
            file_buffer=file_buffer,
        )


def store_or_reuse_blob(
    db: Session,
    storage: StorageBackend,
    *,
    content_hash: str,
    mime_type: str,
    size_bytes: int,
    file_buffer,
) -> tuple[Blob, bool]:
    """Return blob and whether content was deduplicated (already existed).

    Race-safe against GC:

    * If the existing blob is ``purging``, wait **without** holding ``FOR UPDATE``,
      then re-lock. Still purging after the wait → :class:`BlobGcConflictError`
      (never bump ref_count onto a mid-purge row).
    * If ``storage_uri == GC_PURGED_URI``, re-upload bytes and restore a live URI.
    * Concurrent creates that collide on PK fall through to the reuse path via
      savepoint + :class:`IntegrityError` handling.
    """
    peek = db.scalar(select(Blob).where(Blob.content_hash == content_hash))
    if peek is not None and peek.gc_state == GC_STATE_PURGING:
        outcome = _poll_gc_settle(content_hash)
        if outcome == "purging":
            raise BlobGcConflictError(content_hash)
        # "gone" or "idle" — fall through to locked select

    existing = db.scalar(
        select(Blob).where(Blob.content_hash == content_hash).with_for_update()
    )
    if existing is not None:
        if existing.gc_state == GC_STATE_PURGING:
            # Lost the race again after settle — do not reuse.
            raise BlobGcConflictError(content_hash)
        return _reuse_locked_blob(
            db,
            storage,
            existing=existing,
            content_hash=content_hash,
            mime_type=mime_type,
            file_buffer=file_buffer,
        )

    return _create_blob(
        db,
        storage,
        content_hash=content_hash,
        mime_type=mime_type,
        size_bytes=size_bytes,
        file_buffer=file_buffer,
    )
