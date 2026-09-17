"""UC 工作台换票 / HMAC 票 / 闸门（不打真 UC）。"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.config import get_settings
from app.uc.client import UcError, parse_uc_envelope, token_auth
from app.uc.config import get_uc_config, uc_status_payload
from app.uc.session import (
    exchange_uc_token,
    is_uc_session_exempt,
    lookup_jti,
    map_uc_principal,
    mock_token_auth,
    reset_uc_session_for_tests,
    resolve_uc_session,
)
from app.uc.ticket import issue_ticket, parse_ticket, verify_ticket
from tests.conftest import TEST_HEADERS

SECRET = "test-uc-hmac-secret"


def _expired_ticket(uid: str, secret: str) -> str:
    jti = "ab" * 16
    exp = int(time.time()) - 30
    sig = hmac.new(
        secret.encode("utf-8"),
        f"{uid}:{jti}:{exp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v1.{uid}.{jti}.{exp}.{sig}"


def test_hmac_ticket_issue_verify_expire_tamper():
    ticket = issue_ticket("alice.wang", secret=SECRET, ttl_sec=3600)
    parsed = parse_ticket(ticket)
    assert parsed is not None
    assert parsed.user_id == "alice.wang"
    assert parsed.jti
    assert verify_ticket(ticket, secret=SECRET) == "alice.wang"
    assert verify_ticket(ticket, secret="wrong") is None

    tampered = ticket[:-1] + ("b" if ticket.endswith("a") else "a")
    assert verify_ticket(tampered, secret=SECRET) is None
    assert verify_ticket(_expired_ticket("alice.wang", SECRET), secret=SECRET) is None
    assert parse_ticket("v2.nope") is None
    short = ticket.rsplit(".", 1)[0] + ".abcd"
    assert verify_ticket(short, secret=SECRET) is None


def test_uc_envelope_and_principal():
    assert parse_uc_envelope({"code": 0, "data": {"userCode": "u1"}}) == {"userCode": "u1"}
    assert parse_uc_envelope({"code": "0", "data": {"userCode": "u1"}}) == {"userCode": "u1"}
    with pytest.raises(UcError) as exc:
        parse_uc_envelope({"code": 1, "message": "bad token"})
    assert exc.value.status_code == 401

    p = map_uc_principal({"usreCode": "ops01", "username": "ops", "realName": "运维"}, "userCode")
    assert p.user_id == "ops01"
    assert p.display_name == "运维"
    assert map_uc_principal({"username": "only-name"}, "userCode").user_id == "only-name"

    mock = mock_token_auth("local-dev", get_uc_config())
    assert mock["userCode"] == "local-dev"
    cfg = get_uc_config()
    assert mock_token_auth("jwt.like.token", cfg)["userCode"] == cfg.mock_user_id


def test_exempt_paths():
    assert is_uc_session_exempt("GET", "/api/auth/uc/status") is True
    assert is_uc_session_exempt("POST", "/api/auth/uc/exchange") is True
    assert is_uc_session_exempt("GET", "/health") is True
    assert is_uc_session_exempt("GET", "/metrics") is True
    assert is_uc_session_exempt("GET", "/dashboard.html") is True
    assert is_uc_session_exempt("GET", "/shared/js/uc.js") is True
    assert is_uc_session_exempt("GET", "/auth") is True
    assert is_uc_session_exempt("GET", "/v1/files") is False
    assert is_uc_session_exempt("POST", "/v1/files") is False


def test_alias_env_dashboard(monkeypatch):
    monkeypatch.delenv("FILE_SERVICE_UC_ENABLED", raising=False)
    monkeypatch.delenv("FILE_SERVICE_UC_MOCK", raising=False)
    monkeypatch.setenv("DASHBOARD_UC_ENABLED", "1")
    monkeypatch.setenv("DASHBOARD_UC_MOCK", "true")
    monkeypatch.setenv("DASHBOARD_UC_BASE_URL", "http://uc.example")
    assert get_uc_config().enabled is True
    assert get_uc_config().mock is True
    assert get_uc_config().base_url == "http://uc.example"

    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "0")
    assert get_uc_config().enabled is False


def test_ticket_secret_falls_back_to_api_keys(monkeypatch):
    monkeypatch.delenv("FILE_SERVICE_UC_SESSION_SECRET", raising=False)
    monkeypatch.delenv("DASHBOARD_UC_SESSION_SECRET", raising=False)
    monkeypatch.setenv("API_KEYS", "alpha-key,beta-key")
    get_settings.cache_clear()
    assert get_uc_config().ticket_secret == "alpha-key"


def test_mock_exchange_and_jti_mutex(monkeypatch):
    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_MOCK", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_SESSION_SECRET", SECRET)
    reset_uc_session_for_tests()

    first = exchange_uc_token("local-dev")
    assert first["user_id"] == "local-dev"
    assert first["session_ticket"].startswith("v1.")
    assert first["source"] == "uc_mock"
    jti1 = parse_ticket(first["session_ticket"]).jti
    assert lookup_jti("local-dev") == jti1
    assert resolve_uc_session(first["session_ticket"]).user_id == "local-dev"

    second = exchange_uc_token("local-dev")
    old = resolve_uc_session(first["session_ticket"])
    assert old.user_id is None
    assert "uc session" in (old.error or "")
    assert resolve_uc_session(second["session_ticket"]).user_id == "local-dev"


def test_exchange_disabled_and_empty_token(monkeypatch):
    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "0")
    with pytest.raises(Exception) as exc:
        exchange_uc_token("local-dev")
    assert exc.value.status_code == 503

    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_MOCK", "1")
    with pytest.raises(Exception) as empty:
        exchange_uc_token("")
    assert empty.value.status_code == 400


def test_token_auth_uses_injected_http(monkeypatch):
    """不打真 UC：httpx 由假客户端替换。"""

    class FakeResp:
        status_code = 200

        def json(self):
            return {"code": 0, "data": {"userCode": "u-http"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, params=None, headers=None):
            assert "/uc/api/login/token-auth" in url
            assert params["token"] == "plat-jwt"
            return FakeResp()

    monkeypatch.setattr("app.uc.client.httpx.Client", FakeClient)
    data = token_auth("plat-jwt", base_url="http://uc.test", timeout_ms=1000)
    assert data["userCode"] == "u-http"


def test_http_gate_off_on_exempt_and_machine(client, monkeypatch):
    test_client, _ = client

    off = test_client.get("/v1/files")
    assert off.status_code == 401
    assert "Authorization" in off.json()["detail"]

    status_off = test_client.get("/api/auth/uc/status")
    assert status_off.status_code == 200
    assert status_off.json()["enabled"] is False

    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_MOCK", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_SESSION_SECRET", SECRET)
    reset_uc_session_for_tests()

    assert test_client.get("/health").status_code == 200
    assert test_client.get("/metrics").status_code == 200
    assert test_client.get("/api/auth/uc/status").json()["enabled"] is True
    assert uc_status_payload()["enabled"] is True

    no_ticket = test_client.get("/v1/files")
    assert no_ticket.status_code == 401
    assert "uc session required" in no_ticket.json()["detail"]

    machine = test_client.get("/v1/files", headers=TEST_HEADERS)
    assert machine.status_code == 200

    bad = test_client.post("/api/auth/uc/exchange", json={"token": ""})
    assert bad.status_code == 400

    exchanged = test_client.post("/api/auth/uc/exchange", json={"token": "local-dev"})
    assert exchanged.status_code == 200
    me = exchanged.json()
    assert me["session_ticket"]

    ticket_only = test_client.get(
        "/v1/files",
        headers={"X-UC-Session": me["session_ticket"], "X-User-Id": me["user_id"]},
    )
    assert ticket_only.status_code == 401
    assert "Authorization" in ticket_only.json()["detail"]

    both = test_client.get(
        "/v1/files",
        headers={
            **TEST_HEADERS,
            "X-UC-Session": me["session_ticket"],
            "X-User-Id": me["user_id"],
        },
    )
    assert both.status_code == 200

    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "0")
    disabled = test_client.post("/api/auth/uc/exchange", json={"token": "local-dev"})
    assert disabled.status_code == 503


def test_http_gate_rejects_stale_jti(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_MOCK", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_SESSION_SECRET", SECRET)
    reset_uc_session_for_tests()

    first = test_client.post("/api/auth/uc/exchange", json={"token": "local-dev"}).json()
    second = test_client.post("/api/auth/uc/exchange", json={"token": "local-dev"}).json()

    stale = test_client.get(
        "/v1/files",
        headers={"X-UC-Session": first["session_ticket"]},
    )
    assert stale.status_code == 401
    assert "uc session invalid or expired" in stale.json()["detail"]

    # 新票过闸后仍须 L0 Bearer
    fresh = test_client.get(
        "/v1/files",
        headers={"X-UC-Session": second["session_ticket"]},
    )
    assert fresh.status_code == 401
    assert "Authorization" in fresh.json()["detail"]


def test_auth_html_and_shared_uc_assets(client):
    test_client, _ = client
    auth = test_client.get("/auth")
    assert auth.status_code == 200
    body = auth.content.decode("utf-8")
    assert "uc-gate" in body or "统一登录" in body
    strip = test_client.get("/shared/js/uc-strip.js")
    assert strip.status_code == 200
    assert b"workbench_token" in strip.content


def test_disabled_uc_user_rejected(monkeypatch):
    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "1")
    monkeypatch.setenv("FILE_SERVICE_UC_MOCK", "0")
    monkeypatch.setenv("FILE_SERVICE_UC_SESSION_SECRET", SECRET)

    class FakeClient:
        def token_auth(self, token, *, base_url, timeout_ms=10000):
            return {"userCode": "gone", "status": 2}

    from app.uc.session import UcExchangeError

    with pytest.raises(UcExchangeError) as exc:
        exchange_uc_token("any", client=FakeClient())
    assert exc.value.status_code == 403
