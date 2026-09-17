import io

from tests.conftest import TEST_HEADERS


def test_batch_status_and_content(client):
    test_client, _ = client
    ids = []
    for i in range(2):
        up = test_client.post(
            "/v1/files",
            headers=TEST_HEADERS,
            files={"file": (f"f{i}.txt", io.BytesIO(f"content {i}".encode()), "text/plain")},
        )
        assert up.status_code == 201
        ids.append(up.json()["reference_id"])

    missing = "00000000-0000-4000-8000-000000000099"
    st = test_client.post(
        "/v1/files/batch-status",
        headers=TEST_HEADERS,
        json={"reference_ids": ids + [missing]},
    )
    assert st.status_code == 200
    body = st.json()
    assert body["total"] == 3
    assert sum(1 for it in body["items"] if it["ok"]) == 2
    assert any(it.get("error_code") == "not_found" for it in body["items"])

    ct = test_client.post(
        "/v1/files/batch-content",
        headers=TEST_HEADERS,
        json={"reference_ids": ids, "profile": "layout", "max_chars": 50},
    )
    assert ct.status_code == 200
    assert ct.json()["total"] == 2
    assert all(it["ok"] for it in ct.json()["items"])
