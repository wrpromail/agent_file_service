import io
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.storage import GC_PURGED_URI, blob_storage_key, custom_parse_storage_key
from app.models.blob import GC_STATE_IDLE, GC_STATE_PURGING, Blob
from app.models.custom_parse_job import CustomParseJob
from app.models.file_reference import FileReference
from app.models.parse_result import ParseResult
from app.services.lifecycle_service import (
    expire_stale_references,
    reclaim_stuck_work,
    run_blob_gc,
)
from tests.conftest import upload_bytes


def test_expire_stale_references(client, auth_headers, db_session):
    test_client, storage = client
    upload = upload_bytes(
        client,
        auth_headers,
        b"expired",
        name="expire-me.txt",
        session_id="expiry-session",
    )
    reference_id = upload.json()["reference_id"]
    content_hash = upload.json()["content_hash"]

    reference = db_session.scalar(
        select(FileReference).where(FileReference.id == uuid.UUID(reference_id))
    )
    reference.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    expired_count = expire_stale_references(db_session, storage)
    db_session.commit()

    assert expired_count == 1
    db_session.refresh(reference)
    assert reference.deleted_at is not None

    detail = test_client.get(f"/v1/files/{reference_id}", headers=auth_headers)
    assert detail.status_code == 404

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.ref_count == 0
    assert blob.storage_uri == GC_PURGED_URI


def test_run_blob_gc_cleans_zero_ref_blob(client, auth_headers, db_session):
    test_client, storage = client
    upload = upload_bytes(client, auth_headers, b"orphan", name="orphan.txt")
    content_hash = upload.json()["content_hash"]

    reference = db_session.scalar(
        select(FileReference).where(FileReference.id == uuid.UUID(upload.json()["reference_id"]))
    )
    reference.deleted_at = datetime.now(timezone.utc)
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    blob.ref_count = 0
    db_session.commit()

    gc_count = run_blob_gc(db_session, storage)
    db_session.commit()

    assert gc_count == 1
    db_session.expire_all()
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.storage_uri == GC_PURGED_URI
    assert not any(key.startswith("blobs/") for key in storage._objects)


def test_reclaim_stuck_processing_parse(client, auth_headers, db_session, monkeypatch):
    monkeypatch.setenv("GC_PROCESSING_RECLAIM_SECONDS", "60")
    from app.config import get_settings

    get_settings.cache_clear()

    upload = upload_bytes(
        client, auth_headers, b"stuck parse", name="stuck.txt", auto_parse="false"
    )
    content_hash = upload.json()["content_hash"]
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    row = ParseResult(
        content_hash=content_hash,
        parser_name="simple",
        parser_version="0.1.0",
        profile="layout",
        status="processing",
        created_at=stale,
        processing_started_at=stale,
    )
    db_session.add(row)
    db_session.commit()

    test_client, storage = client
    stats = reclaim_stuck_work(db_session, storage)
    db_session.commit()

    assert stats["parse_reclaimed"] == 1
    db_session.refresh(row)
    assert row.status == "failed"
    assert "Reclaimed" in (row.error or "")


def test_reclaim_stuck_purging_with_active_ref(client, auth_headers, db_session):
    test_client, storage = client
    upload = upload_bytes(client, auth_headers, b"mid purge keep", name="keep.txt", auto_parse="false")
    content_hash = upload.json()["content_hash"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    blob.gc_state = GC_STATE_PURGING
    storage.delete_object(blob_storage_key(content_hash))
    db_session.commit()

    stats = reclaim_stuck_work(db_session, storage)
    db_session.commit()

    assert stats["purging_reclaimed"] == 1
    db_session.expire_all()
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob.gc_state == GC_STATE_IDLE
    assert blob.storage_uri == GC_PURGED_URI
    assert blob.ref_count == 1


def test_reclaim_stuck_purging_zero_ref_finishes(client, auth_headers, db_session):
    test_client, storage = client
    upload = upload_bytes(client, auth_headers, b"finish purge", name="fin.txt", auto_parse="false")
    content_hash = upload.json()["content_hash"]

    reference = db_session.scalar(
        select(FileReference).where(FileReference.id == uuid.UUID(upload.json()["reference_id"]))
    )
    reference.deleted_at = datetime.now(timezone.utc)
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    blob.ref_count = 0
    blob.gc_state = GC_STATE_PURGING
    db_session.commit()

    stats = reclaim_stuck_work(db_session, storage)
    db_session.commit()

    assert stats["purging_reclaimed"] == 1
    db_session.expire_all()
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.gc_state == GC_STATE_IDLE
    assert blob.storage_uri == GC_PURGED_URI
    assert not any(key.startswith("blobs/") for key in storage._objects)


def test_gc_purges_custom_parse_and_mineru_workdir(
    client, auth_headers, db_session, monkeypatch, tmp_path
):
    monkeypatch.setenv("MINERU_WORK_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()

    test_client, storage = client
    upload = upload_bytes(
        client, auth_headers, b"purge extras", name="extras.txt", auto_parse="false"
    )
    body = upload.json()
    content_hash = body["content_hash"]
    reference_id = uuid.UUID(body["reference_id"])

    job_id = uuid.uuid4()
    result_key = custom_parse_storage_key(str(job_id))
    storage.put_object(result_key, io.BytesIO(b'{"text":"x"}'), "application/json")
    job = CustomParseJob(
        id=job_id,
        reference_id=reference_id,
        content_hash=content_hash,
        tenant_id="tenant-a",
        media_kind="image",
        prompt="extract",
        status="completed",
        parser_name="custom",
        parser_version="0.1.0",
        result_uri=f"memory://{result_key}",
        result_text="x",
    )
    db_session.add(job)

    work = tmp_path / content_hash[:16]
    work.mkdir(parents=True)
    (work / "full.md").write_text("mineru output", encoding="utf-8")
    db_session.commit()

    delete = test_client.delete(f"/v1/files/{reference_id}", headers=auth_headers)
    assert delete.status_code == 204

    db_session.expire_all()
    assert db_session.get(CustomParseJob, job_id) is None
    assert result_key not in storage._objects
    assert not work.exists()
