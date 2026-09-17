from sqlalchemy import select

from app.config import get_settings
from app.models.blob import Blob
from tests.conftest import upload_bytes


def test_upload_returns_reference(client, auth_headers):
    response = upload_bytes(client, auth_headers, b"hello file service", name="notes.txt")
    assert response.status_code == 201
    body = response.json()
    assert body["reference_id"]
    assert body["content_hash"]
    assert body["deduplicated"] is False
    assert body["metadata"]["filename"] == "notes.txt"
    assert body["metadata"]["size_bytes"] == 18
    assert body["parse_status"] == "completed"


def test_upload_rejects_when_exceeds_max_upload_bytes(client, auth_headers, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "32")
    get_settings.cache_clear()
    response = upload_bytes(
        client,
        auth_headers,
        b"x" * 64,
        name="big.bin",
        mime="application/octet-stream",
    )
    assert response.status_code == 413
    detail = response.json().get("detail") or ""
    assert "max_upload_bytes" in detail or "上限" in detail


def test_get_file_detail(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(
        client,
        auth_headers,
        b"hello file service",
        name="notes.txt",
        session_id="sess-1",
        metadata='{"source":"test"}',
    )
    reference_id = upload.json()["reference_id"]

    response = test_client.get(f"/v1/files/{reference_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["session_id"] == "sess-1"
    assert body["custom_metadata"] == {"source": "test"}
    assert any(
        item["profile"] == "metadata" and item["status"] == "completed"
        for item in body["parse_results"]
    )


def test_upload_includes_processing_routes(client, auth_headers):
    test_client, _ = client
    response = upload_bytes(
        client,
        auth_headers,
        b"%PDF-1.4\n",
        name="scan.pdf",
        mime="application/pdf",
        auto_parse="false",
    )
    assert response.status_code == 201
    detail = test_client.get(
        f"/v1/files/{response.json()['reference_id']}",
        headers=auth_headers,
    )
    metadata_result = next(
        item for item in detail.json()["parse_results"] if item["profile"] == "metadata"
    )
    assert metadata_result["status"] == "completed"


def test_duplicate_upload_deduplicates_blob(client, auth_headers, db_session):
    test_client, storage = client
    payload = b"same content for dedup"

    first = upload_bytes(client, auth_headers, payload, name="a.txt")
    second = upload_bytes(client, auth_headers, payload, name="b.txt")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    assert first.json()["content_hash"] == second.json()["content_hash"]
    assert first.json()["reference_id"] != second.json()["reference_id"]

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == first.json()["content_hash"]))
    assert blob is not None
    assert blob.ref_count == 2
    blob_keys = [key for key in storage._objects if key.startswith("blobs/")]
    assert len(blob_keys) == 1


def test_download_original_file(client, auth_headers):
    test_client, _ = client
    payload = b"download me please"
    upload = upload_bytes(
        client, auth_headers, payload, name="sample.txt", auto_parse="false"
    )
    reference_id = upload.json()["reference_id"]

    response = test_client.get(f"/v1/files/{reference_id}/download", headers=auth_headers)
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"].startswith("text/plain")
    assert "sample.txt" in response.headers.get("content-disposition", "")


def test_download_requires_tenant_ownership(client, auth_headers, other_tenant_headers):
    test_client, _ = client
    upload = upload_bytes(client, auth_headers, b"secret", name="secret.txt")
    reference_id = upload.json()["reference_id"]

    response = test_client.get(
        f"/v1/files/{reference_id}/download", headers=other_tenant_headers
    )
    assert response.status_code == 404
