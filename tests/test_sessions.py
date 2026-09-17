from tests.conftest import TEST_HEADERS


def test_list_session_files(client, auth_headers):
    test_client, _ = client
    session_id = "session-42"

    first = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("one.txt", b"one", "text/plain")},
        data={"session_id": session_id},
    )
    second = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("two.txt", b"two", "text/plain")},
        data={"session_id": session_id},
    )
    test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("other.txt", b"other", "text/plain")},
        data={"session_id": "other-session"},
    )

    response = test_client.get(f"/v1/sessions/{session_id}/files", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert len(body["files"]) == 2
    filenames = {item["original_filename"] for item in body["files"]}
    assert filenames == {"one.txt", "two.txt"}
    reference_ids = {item["reference_id"] for item in body["files"]}
    assert first.json()["reference_id"] in reference_ids
    assert second.json()["reference_id"] in reference_ids


def test_session_files_are_tenant_scoped(client, auth_headers):
    test_client, _ = client
    session_id = "shared-session-id"

    test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("tenant-a.txt", b"a", "text/plain")},
        data={"session_id": session_id},
    )

    response = test_client.get(
        f"/v1/sessions/{session_id}/files",
        headers={**TEST_HEADERS, "X-Tenant-Id": "tenant-b"},
    )
    assert response.status_code == 200
    assert response.json()["files"] == []