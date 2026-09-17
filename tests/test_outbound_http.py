"""出站 HTTP 重试策略与 JSON 传输。"""
from unittest.mock import MagicMock

import httpx
import pytest
import requests

from app.config import Settings
from app.integrations.http import (
    RetryableError,
    RetryPolicy,
    call_with_retry,
    retry_policy_from_settings,
)
from app.integrations.http.httpx_json import request_json as httpx_request_json
from app.integrations.http.requests_json import request_json as requests_request_json


def test_retry_policy_from_settings_inherits_outbound():
    settings = Settings(
        outbound_http_max_attempts=4,
        outbound_http_backoff_base=2.0,
        litellm_max_attempts=0,
        mineru_max_attempts=5,
    )
    litellm = retry_policy_from_settings(settings, service="litellm")
    assert litellm.max_attempts == 4
    assert litellm.backoff_base == 2.0

    mineru = retry_policy_from_settings(settings, service="mineru")
    assert mineru.max_attempts == 5


def test_retry_policy_service_override():
    settings = Settings(
        outbound_http_max_attempts=3,
        litellm_max_attempts=2,
        litellm_backoff_base=0.5,
    )
    policy = retry_policy_from_settings(settings, service="litellm")
    assert policy.max_attempts == 2
    assert policy.backoff_base == 0.5


def test_call_with_retry_succeeds_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("temp", status_code=503)
        return "ok"

    sleeps: list[float] = []
    result = call_with_retry(
        flaky,
        policy=RetryPolicy(max_attempts=3, backoff_base=0.0, backoff_max=0.0),
        sleep=lambda s: sleeps.append(s),
    )
    assert result == "ok"
    assert calls["n"] == 3


def test_call_with_retry_exhausted():
    def always_fail():
        raise RetryableError("nope", status_code=502)

    with pytest.raises(RetryableError, match="nope"):
        call_with_retry(
            always_fail,
            policy=RetryPolicy(max_attempts=2, backoff_base=0.0),
            sleep=lambda _s: None,
        )


def test_httpx_request_json_retries_429(monkeypatch):
    policy = RetryPolicy(max_attempts=3, backoff_base=0.0, rate_limit_wait=0.0)
    bodies = [
        (429, "rate limit", None),
        (200, '{"ok": true}', {"ok": True}),
    ]
    state = {"i": 0}

    class FakeResponse:
        def __init__(self, status, text, data):
            self.status_code = status
            self.text = text
            self._data = data

        def json(self):
            if self._data is None:
                raise ValueError("bad")
            return self._data

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            i = state["i"]
            state["i"] += 1
            status, text, data = bodies[i]
            return FakeResponse(status, text, data)

        def close(self):
            pass

    monkeypatch.setattr("app.integrations.http.httpx_json.httpx.Client", FakeClient)
    data = httpx_request_json(
        "POST",
        "http://example/v1",
        json={"a": 1},
        policy=policy,
        sleep=lambda _s: None,
        error_prefix="Test",
    )
    assert data == {"ok": True}
    assert state["i"] == 2


def test_httpx_request_json_no_retry_on_400():
    policy = RetryPolicy(max_attempts=3, backoff_base=0.0)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 400
    mock_response.text = "bad request"

    mock_http = MagicMock(spec=httpx.Client)
    mock_http.request.return_value = mock_response
    mock_http.close = MagicMock()

    from unittest.mock import patch

    with patch("app.integrations.http.httpx_json.httpx.Client", return_value=mock_http):
        with pytest.raises(RuntimeError, match="HTTP 400"):
            httpx_request_json(
                "POST",
                "http://example/v1",
                policy=policy,
                sleep=lambda _s: None,
            )
    assert mock_http.request.call_count == 1


def test_requests_json_retries_5xx():
    policy = RetryPolicy(max_attempts=3, backoff_base=0.0)
    session = MagicMock(spec=requests.Session)

    fail = MagicMock()
    fail.status_code = 500
    fail.text = "err"

    ok = MagicMock()
    ok.status_code = 200
    ok.text = '{"batch": 1}'
    ok.json.return_value = {"batch": 1}

    session.request.side_effect = [fail, ok]

    data = requests_request_json(
        session,
        "GET",
        "http://mineru.example/x",
        policy=policy,
        sleep=lambda _s: None,
        error_prefix="MinerU",
    )
    assert data == {"batch": 1}
    assert session.request.call_count == 2
