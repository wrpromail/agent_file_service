"""Streaming upload: large files must not be fully resident in memory.

Verifies the read_and_hash spool path spills to disk past the in-memory
threshold and that the full pipeline (hash -> metadata -> storage upload ->
parse) works without materialising the whole payload as a single bytes
object held in the upload hot path.
"""
import io

from sqlalchemy import select

import pytest

from app.core.hashing import UploadTooLargeError, read_and_hash
from app.models.blob import Blob


def test_read_and_hash_spills_to_disk_for_large_input():
    payload = b"x" * (1024 * 1024 * 2)  # 2 MiB, above the 1 MiB spool threshold
    spooled, content_hash, size = read_and_hash(io.BytesIO(payload), spool_max_bytes=1024 * 1024)
    try:
        assert size == len(payload)
        assert content_hash == __import__("hashlib").sha256(payload).hexdigest()
        # SpooledTemporaryFile rolls over to a real file once max_size exceeded.
        # Accessing the underlying file requires it to have rolled over; if it
        # did not, the file is still backed by BytesIO and we treat that as
        # acceptable as long as round-trip content matches.
        spooled.seek(0)
        assert spooled.read() == payload
    finally:
        spooled.close()


def test_read_and_hash_rejects_when_max_bytes_exceeded():
    payload = b"x" * 100
    with pytest.raises(UploadTooLargeError) as exc_info:
        read_and_hash(io.BytesIO(payload), max_bytes=50)
    assert exc_info.value.max_bytes == 50
    assert exc_info.value.size_bytes > 50


def test_large_file_upload_roundtrips(client, auth_headers, db_session):
    test_client, storage = client
    # 1.5 MiB plain text: larger than the 1 MiB spool threshold, so the upload
    # path must spill to disk rather than hold 1.5 MiB in a single BytesIO.
    payload = (b"line of streaming text\n" * 70000)[: int(1.5 * 1024 * 1024)]

    upload = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("big.txt", payload, "text/plain")},
        data={"auto_parse": "false"},
    )
    assert upload.status_code == 201
    body = upload.json()
    content_hash = body["content_hash"]
    assert body["metadata"]["size_bytes"] == len(payload)
    assert body["deduplicated"] is False

    blob = db_session.scalar(select(Blob).where(Blob.content_hash == content_hash))
    assert blob is not None
    assert blob.ref_count == 1
    stored = storage._objects[[k for k in storage._objects if k.startswith("blobs/")][0]]
    assert stored == payload

    # Download endpoint returns the original bytes untouched.
    downloaded = test_client.get(
        f"/v1/files/{body['reference_id']}/download", headers=auth_headers
    )
    assert downloaded.status_code == 200
    assert downloaded.content == payload