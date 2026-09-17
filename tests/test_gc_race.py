"""GC vs upload race: a blob purged by GC must be re-materialised on the next
upload of the same content, so the new reference never points at purged storage.
"""
import io

import pytest
from sqlalchemy import select

from app.core.storage import GC_PURGED_URI, blob_storage_key
from app.models.blob import GC_STATE_PURGING, Blob
from app.services.dedup import BlobGcConflictError, store_or_reuse_blob
from tests.conftest import upload_bytes


def test_reuse_after_gc_purge_recreates_storage(client, auth_headers, db_session):
    """Scenario: blob was GC-purged (storage deleted, uri=gc://purged) but the
    row survives. A new upload of the same content must re-upload the bytes and
    restore a real storage_uri — the new reference must not see purged storage.
    """
    test_client, storage = client
    payload = b"race-condition content"
    upload = upload_bytes(client, auth_headers, payload, auto_parse="false")
    content_hash = upload.json()["content_hash"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    # Simulate a GC pass that purged storage but left the row (e.g. a reference
    # reappeared mid-purge so the row survived with storage_uri=gc://purged).
    storage.delete_object(blob_storage_key(content_hash))
    blob.storage_uri = GC_PURGED_URI
    blob.ref_count = 0
    db_session.commit()

    assert not any(k.startswith("blobs/") for k in storage._objects)

    # New upload of identical content.
    second = upload_bytes(client, auth_headers, payload, name="again.txt", auto_parse="false")
    assert second.status_code == 201
    assert second.json()["deduplicated"] is True
    assert second.json()["content_hash"] == content_hash

    db_session.expire_all()
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.ref_count == 1
    assert blob.storage_uri != GC_PURGED_URI
    assert (
        blob.storage_uri.startswith("memory://blobs/")
        or blob.storage_uri.startswith("s3://")
        or blob.storage_uri.startswith("local://blobs/")
    )
    # Storage bytes are live again.
    stored = storage._objects[blob_storage_key(content_hash)]
    assert stored == payload

    # The new reference can download the original bytes.
    downloaded = test_client.get(
        f"/v1/files/{second.json()['reference_id']}/download", headers=auth_headers
    )
    assert downloaded.status_code == 200
    assert downloaded.content == payload


def test_store_or_reuse_blob_reuploads_purged_storage_directly(client, auth_headers, db_session):
    """Unit-level: calling store_or_reuse_blob on a purged row re-uploads bytes
    and restores storage_uri (does not just bump ref_count on dead storage).
    """
    test_client, storage = client
    payload = b"direct purged reuse"
    upload = upload_bytes(client, auth_headers, payload, auto_parse="false")
    content_hash = upload.json()["content_hash"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    storage.delete_object(blob_storage_key(content_hash))
    blob.storage_uri = GC_PURGED_URI
    blob.ref_count = 0
    db_session.commit()

    blob, deduplicated = store_or_reuse_blob(
        db_session,
        storage,
        content_hash=content_hash,
        mime_type="text/plain",
        size_bytes=len(payload),
        file_buffer=io.BytesIO(payload),
    )
    db_session.commit()
    assert deduplicated is True
    assert blob.ref_count == 1
    assert blob.storage_uri != GC_PURGED_URI
    assert storage._objects[blob_storage_key(content_hash)] == payload


def test_gc_does_not_purge_blob_with_active_reference(client, auth_headers, db_session):
    """If ref_count > 0, gc_blob must leave storage untouched (no partial purge
    that would dangle references)."""
    test_client, storage = client
    upload = upload_bytes(client, auth_headers, b"keep me", auto_parse="false")
    content_hash = upload.json()["content_hash"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob.ref_count == 1

    from app.services.lifecycle_service import gc_blob

    purged = gc_blob(db_session, storage, content_hash)
    db_session.commit()
    assert purged is False
    db_session.expire_all()
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.ref_count == 1
    assert blob.storage_uri != GC_PURGED_URI
    assert storage._objects[blob_storage_key(content_hash)] == b"keep me"


def test_store_or_reuse_refuses_stuck_purging(client, auth_headers, db_session, monkeypatch):
    """If GC stays in purging past the settle window, reuse must fail — never
    bump ref_count onto a mid-purge row."""
    test_client, storage = client
    payload = b"stuck purging"
    upload = upload_bytes(client, auth_headers, payload, auto_parse="false")
    content_hash = upload.json()["content_hash"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    blob.gc_state = GC_STATE_PURGING
    blob.ref_count = 0
    db_session.commit()

    # Pin the settle path: production SessionLocal is a different DB in tests.
    monkeypatch.setattr("app.services.dedup._poll_gc_settle", lambda _hash: "purging")

    with pytest.raises(BlobGcConflictError):
        store_or_reuse_blob(
            db_session,
            storage,
            content_hash=content_hash,
            mime_type="text/plain",
            size_bytes=len(payload),
            file_buffer=io.BytesIO(payload),
        )


def test_upload_returns_409_when_blob_stuck_purging(client, auth_headers, db_session, monkeypatch):
    payload = b"upload conflict purging"
    upload = upload_bytes(client, auth_headers, payload, auto_parse="false")
    content_hash = upload.json()["content_hash"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    blob.gc_state = GC_STATE_PURGING
    blob.ref_count = 0
    db_session.commit()

    monkeypatch.setattr("app.services.dedup._poll_gc_settle", lambda _hash: "purging")

    second = upload_bytes(client, auth_headers, payload, name="again.txt", auto_parse="false")
    assert second.status_code == 409


def test_clone_rejects_purged_blob(client, auth_headers, db_session):
    test_client, storage = client
    upload = upload_bytes(client, auth_headers, b"clone purged", auto_parse="false")
    body = upload.json()
    content_hash = body["content_hash"]
    reference_id = body["reference_id"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    blob.storage_uri = GC_PURGED_URI
    db_session.commit()

    clone = test_client.post(
        f"/v1/files/{reference_id}/clone",
        headers=auth_headers,
        json={"target_tenant_id": "tenant-b"},
    )
    assert clone.status_code == 409


def test_clone_rejects_purging_blob(client, auth_headers, db_session):
    test_client, storage = client
    upload = upload_bytes(client, auth_headers, b"clone purging", auto_parse="false")
    body = upload.json()
    content_hash = body["content_hash"]
    reference_id = body["reference_id"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    blob.gc_state = GC_STATE_PURGING
    db_session.commit()

    clone = test_client.post(
        f"/v1/files/{reference_id}/clone",
        headers=auth_headers,
        json={"target_tenant_id": "tenant-b"},
    )
    assert clone.status_code == 409
