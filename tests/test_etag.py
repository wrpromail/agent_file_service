import io

from tests.conftest import TEST_HEADERS


def test_content_etag_304(client):
    test_client, _ = client
    up = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("e.txt", io.BytesIO(b"etag body"), "text/plain")},
    )
    ref = up.json()["reference_id"]
    r1 = test_client.get(f"/v1/files/{ref}/content?profile=layout", headers=TEST_HEADERS)
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag

    r2 = test_client.get(
        f"/v1/files/{ref}/content?profile=layout",
        headers={**TEST_HEADERS, "If-None-Match": etag},
    )
    assert r2.status_code == 304


def test_status_etag_304(client):
    test_client, _ = client
    up = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("s.txt", io.BytesIO(b"status"), "text/plain")},
    )
    ref = up.json()["reference_id"]
    r1 = test_client.get(f"/v1/files/{ref}/status", headers=TEST_HEADERS)
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    r2 = test_client.get(
        f"/v1/files/{ref}/status",
        headers={**TEST_HEADERS, "If-None-Match": etag},
    )
    assert r2.status_code == 304
