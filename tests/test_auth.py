"""API key auth (always required) and optional tenant allowlist."""

from app.config import Settings, get_settings
from app.core.security import api_key_is_allowed, ensure_api_keys, parse_api_keys
from tests.conftest import TEST_API_KEY, TEST_HEADERS


def test_missing_authorization_returns_401(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/files",
        headers={"X-Tenant-Id": "tenant-a"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401
    assert "Authorization" in response.json()["detail"]


def test_invalid_api_key_returns_401(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/files",
        headers={
            "Authorization": "Bearer wrong-key",
            "X-Tenant-Id": "tenant-a",
        },
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid API key"


def test_multiple_api_keys_any_accepted(client, monkeypatch):
    monkeypatch.setenv("API_KEYS", "key-one,key-two")
    get_settings.cache_clear()

    test_client, _ = client
    for key in ("key-one", "key-two"):
        response = test_client.post(
            "/v1/files",
            headers={
                "Authorization": f"Bearer {key}",
                "X-Tenant-Id": "tenant-a",
            },
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 201, response.text


def test_api_key_tenant_map_restricts(client, monkeypatch):
    monkeypatch.setenv("API_KEYS", TEST_API_KEY)
    monkeypatch.setenv("API_KEY_TENANT_MAP", f"{TEST_API_KEY}:tenant-a")
    get_settings.cache_clear()

    test_client, _ = client
    ok = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert ok.status_code == 201, ok.text

    denied = test_client.post(
        "/v1/files",
        headers={
            "Authorization": f"Bearer {TEST_API_KEY}",
            "X-Tenant-Id": "tenant-other",
        },
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert denied.status_code == 403
    assert "tenant" in denied.json()["detail"].lower()


def test_ensure_api_keys_generates_when_empty():
    settings = Settings(api_keys="")
    assert settings.allowed_api_keys == set()
    generated = ensure_api_keys(settings)
    assert generated is not None
    assert len(generated) >= 32
    assert settings.allowed_api_keys == {generated}
    # Second call must not regenerate
    assert ensure_api_keys(settings) is None
    assert settings.api_keys == generated


def test_ensure_api_keys_noop_when_configured():
    settings = Settings(api_keys="a,b")
    assert ensure_api_keys(settings) is None
    assert settings.allowed_api_keys == {"a", "b"}


def test_empty_api_keys_generated_key_accepted(client, monkeypatch):
    """Empty API_KEYS + ensure_api_keys → generated key works on business APIs."""
    monkeypatch.setenv("API_KEYS", "")
    get_settings.cache_clear()
    settings = get_settings()
    generated = ensure_api_keys(settings)
    assert generated

    test_client, _ = client
    response = test_client.post(
        "/v1/files",
        headers={
            "Authorization": f"Bearer {generated}",
            "X-Tenant-Id": "tenant-a",
        },
        files={"file": ("notes.txt", b"ephemeral", "text/plain")},
    )
    assert response.status_code == 201, response.text


def test_api_key_is_allowed_helpers():
    allowed = parse_api_keys("alpha,beta")
    assert api_key_is_allowed("alpha", allowed)
    assert api_key_is_allowed("beta", allowed)
    assert not api_key_is_allowed("gamma", allowed)
    assert not api_key_is_allowed("alp", allowed)
    assert not api_key_is_allowed("", allowed)
    assert not api_key_is_allowed("alpha", set())


def test_tenant_allowlist_rejects_unknown(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_TENANT_IDS", "tenant-a,tenant-c")
    get_settings.cache_clear()

    test_client, _ = client
    denied = test_client.post(
        "/v1/files",
        headers={
            "Authorization": f"Bearer {TEST_API_KEY}",
            "X-Tenant-Id": "tenant-b",
        },
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Tenant not allowed"

    allowed = test_client.post(
        "/v1/files",
        headers={
            "Authorization": f"Bearer {TEST_API_KEY}",
            "X-Tenant-Id": "tenant-a",
        },
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert allowed.status_code == 201


def test_tenant_allowlist_unset_allows_any(client, auth_headers):
    test_client, _ = client
    # ALLOWED_TENANT_IDS empty in conftest
    headers = {**auth_headers, "X-Tenant-Id": "any-caller-tenant"}
    response = test_client.post(
        "/v1/files",
        headers=headers,
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 201
    reference_id = response.json()["reference_id"]
    detail = test_client.get(f"/v1/files/{reference_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["tenant_id"] == "any-caller-tenant"


def test_clone_target_must_be_on_allowlist(client, auth_headers, monkeypatch):
    monkeypatch.setenv("ALLOWED_TENANT_IDS", "tenant-a,tenant-b")
    get_settings.cache_clear()

    test_client, _ = client
    upload = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("notes.txt", b"clone-me", "text/plain")},
    )
    assert upload.status_code == 201
    reference_id = upload.json()["reference_id"]

    denied = test_client.post(
        f"/v1/files/{reference_id}/clone",
        headers=auth_headers,
        json={"target_tenant_id": "tenant-evil"},
    )
    assert denied.status_code == 403

    ok = test_client.post(
        f"/v1/files/{reference_id}/clone",
        headers=auth_headers,
        json={"target_tenant_id": "tenant-b", "session_id": "s-b"},
    )
    assert ok.status_code == 201
    assert ok.json()["target_tenant_id"] == "tenant-b"


def test_health_does_not_require_auth(client):
    test_client, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200


def test_fastapi_docs_and_openapi_disabled(client):
    """生产关闭交互式文档与 schema，避免未鉴权暴露面。"""
    test_client, _ = client
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert test_client.get(path).status_code == 404


def test_settings_tenant_allowlist_property():
    assert Settings(allowed_tenant_ids="").tenant_allowlist is None
    assert Settings(allowed_tenant_ids="  ").tenant_allowlist is None
    assert Settings(allowed_tenant_ids="a, b").tenant_allowlist == {"a", "b"}


def test_cross_tenant_access_denied(client, auth_headers, other_tenant_headers):
    test_client, _ = client
    upload = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("secret.txt", b"tenant-a-secret", "text/plain")},
    )
    reference_id = upload.json()["reference_id"]

    detail = test_client.get(f"/v1/files/{reference_id}", headers=other_tenant_headers)
    status = test_client.get(f"/v1/files/{reference_id}/status", headers=other_tenant_headers)

    assert detail.status_code == 404
    assert status.status_code == 404


def test_missing_tenant_header_returns_401(client):
    test_client, _ = client
    response = test_client.post(
        "/v1/files",
        headers={"Authorization": TEST_HEADERS["Authorization"]},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401
