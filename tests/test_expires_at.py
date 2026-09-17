from datetime import datetime, timedelta, timezone

import pytest

from app.core.datetime_utils import parse_expires_at


def test_parse_expires_at_accepts_iso_z():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    raw = future.strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = parse_expires_at(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_expires_at_rejects_past():
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with pytest.raises(ValueError, match="future"):
        parse_expires_at(past)


def test_upload_with_expires_at(client, auth_headers):
    test_client, _ = client
    future = datetime.now(timezone.utc) + timedelta(days=7)
    expires_raw = future.strftime("%Y-%m-%dT%H:%M:%SZ")

    upload = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("ttl.txt", b"expires later", "text/plain")},
        data={"expires_at": expires_raw},
    )
    assert upload.status_code == 201
    assert upload.json()["expires_at"] is not None

    detail = test_client.get(
        f"/v1/files/{upload.json()['reference_id']}",
        headers=auth_headers,
    )
    assert detail.status_code == 200
    assert detail.json()["expires_at"] is not None


def test_upload_rejects_invalid_expires_at(client, auth_headers):
    test_client, _ = client
    response = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("bad.txt", b"bad ttl", "text/plain")},
        data={"expires_at": "not-a-date"},
    )
    assert response.status_code == 400