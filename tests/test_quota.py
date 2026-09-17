import io

from app.config import get_settings
from tests.conftest import TEST_HEADERS


def test_file_count_quota(client, monkeypatch):
    monkeypatch.setenv("QUOTA_MAX_FILES_PER_TENANT", "1")
    get_settings.cache_clear()
    test_client, _ = client

    r1 = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("a.txt", io.BytesIO(b"one"), "text/plain")},
    )
    assert r1.status_code == 201

    r2 = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("b.txt", io.BytesIO(b"two"), "text/plain")},
    )
    assert r2.status_code == 429
    detail = r2.json()["detail"]
    assert detail["message_code"] == "quota_exceeded_files"
