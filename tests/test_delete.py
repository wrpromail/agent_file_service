from sqlalchemy import select

from app.core.storage import GC_PURGED_URI
from app.models.blob import Blob
from tests.conftest import upload_bytes


def test_delete_reference_returns_404_on_get(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(client, auth_headers, b"to be deleted", name="remove-me.txt")
    reference_id = upload.json()["reference_id"]

    delete = test_client.delete(f"/v1/files/{reference_id}", headers=auth_headers)
    assert delete.status_code == 204

    detail = test_client.get(f"/v1/files/{reference_id}", headers=auth_headers)
    assert detail.status_code == 404


def test_delete_last_reference_gcs_blob(client, auth_headers, db_session):
    test_client, storage = client
    upload = upload_bytes(client, auth_headers, b"gc content", name="gc-me.txt")
    content_hash = upload.json()["content_hash"]
    reference_id = upload.json()["reference_id"]

    delete = test_client.delete(f"/v1/files/{reference_id}", headers=auth_headers)
    assert delete.status_code == 204

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.ref_count == 0
    assert blob.storage_uri == GC_PURGED_URI
    assert not any(key.startswith("blobs/") for key in storage._objects)


def test_delete_one_of_two_references_keeps_blob(client, auth_headers, db_session):
    test_client, storage = client
    payload = b"shared gc content"

    first = upload_bytes(client, auth_headers, payload, name="a.txt")
    second = upload_bytes(client, auth_headers, payload, name="b.txt")
    content_hash = first.json()["content_hash"]

    delete = test_client.delete(
        f"/v1/files/{first.json()['reference_id']}",
        headers=auth_headers,
    )
    assert delete.status_code == 204

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.ref_count == 1
    assert any(key.startswith("blobs/") for key in storage._objects)

    delete_second = test_client.delete(
        f"/v1/files/{second.json()['reference_id']}",
        headers=auth_headers,
    )
    assert delete_second.status_code == 204
    db_session.expire_all()
    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.ref_count == 0
    assert blob.storage_uri == GC_PURGED_URI


def test_cross_tenant_cannot_delete(client, auth_headers, other_tenant_headers):
    test_client, _ = client
    upload = upload_bytes(client, auth_headers, b"protected", name="protected.txt")
    reference_id = upload.json()["reference_id"]

    response = test_client.delete(
        f"/v1/files/{reference_id}",
        headers=other_tenant_headers,
    )
    assert response.status_code == 404
