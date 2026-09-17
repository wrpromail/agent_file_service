import io

from tests.conftest import TEST_HEADERS


def test_patch_merge_and_clear(client):
    test_client, _ = client
    up = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("n.txt", io.BytesIO(b"hello patch"), "text/plain")},
        data={"session_id": "s1", "metadata": '{"a":1,"b":2}'},
    )
    assert up.status_code == 201
    ref = up.json()["reference_id"]

    resp = test_client.patch(
        f"/v1/files/{ref}",
        headers=TEST_HEADERS,
        json={"session_id": "s2", "custom_metadata": {"b": 3, "c": 4}, "metadata_mode": "merge"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "s2"
    assert body["custom_metadata"]["a"] == 1
    assert body["custom_metadata"]["b"] == 3
    assert body["custom_metadata"]["c"] == 4

    resp2 = test_client.patch(
        f"/v1/files/{ref}",
        headers=TEST_HEADERS,
        json={"session_id": None, "custom_metadata": {"only": True}, "metadata_mode": "replace"},
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["session_id"] is None
    assert body2["custom_metadata"] == {"only": True}


def test_patch_tenant_isolation(client):
    test_client, _ = client
    up = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("n.txt", io.BytesIO(b"x"), "text/plain")},
    )
    ref = up.json()["reference_id"]
    other = {**TEST_HEADERS, "X-Tenant-Id": "tenant-b"}
    resp = test_client.patch(f"/v1/files/{ref}", headers=other, json={"agent_id": "evil"})
    assert resp.status_code == 404
