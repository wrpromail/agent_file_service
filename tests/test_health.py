from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.core.dependency_health import (
    CheckResult,
    DependencyHealthReport,
    check_litellm,
    run_dependency_checks,
    run_startup_dependency_checks,
)
from app.main import app


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert "dependencies" in body
    assert "checks" in body["dependencies"]
    assert "ok" in body["dependencies"]


def test_health_deep_runs_checks(monkeypatch) -> None:
    """deep=true 应重新探测；mock 依赖检查避免外网。"""
    report = DependencyHealthReport(
        ok=True,
        checked_at=1.0,
        checks=[
            CheckResult(name="database", ok=True, required=True, detail="ok"),
            CheckResult(name="litellm", ok=True, required=True, detail="ok"),
        ],
    )
    with patch("app.main.run_dependency_checks", return_value=report) as mocked:
        client = TestClient(app)
        response = client.get("/health?deep=true")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["dependencies"]["ok"] is True
    mocked.assert_called()


def test_check_litellm_models_present() -> None:
    settings = Settings(
        litellm_base_url="http://litellm.test:4000",
        litellm_api_key="sk-test",
        vision_model="qwen3.5:cloud",
        audio_model="gemini-flash-3.5",
        video_model="gemini-flash-3.5",
    )

    class FakeResp:
        def __init__(self, status_code: int, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_get(url, headers=None):
        if url.endswith("/health/liveliness"):
            return FakeResp(200)
        if url.endswith("/v1/models"):
            return FakeResp(
                200,
                {
                    "data": [
                        {"id": "qwen3.5:cloud"},
                        {"id": "gemini-flash-3.5"},
                    ]
                },
            )
        return FakeResp(404)

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client.get.side_effect = fake_get

    with patch("app.core.dependency_health.httpx.Client", return_value=mock_client):
        result = check_litellm(settings, timeout=2.0)

    assert result.ok is True
    assert "qwen3.5:cloud" in result.detail


def test_check_litellm_missing_model() -> None:
    settings = Settings(
        litellm_base_url="http://litellm.test:4000",
        litellm_api_key="sk-test",
        vision_model="missing-model",
        audio_model="gemini-flash-3.5",
        video_model="gemini-flash-3.5",
    )

    class FakeResp:
        def __init__(self, status_code: int, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_get(url, headers=None):
        if url.endswith("/health/liveliness"):
            return FakeResp(200)
        if url.endswith("/v1/models"):
            return FakeResp(200, {"data": [{"id": "gemini-flash-3.5"}]})
        return FakeResp(404)

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client.get.side_effect = fake_get

    with patch("app.core.dependency_health.httpx.Client", return_value=mock_client):
        result = check_litellm(settings, timeout=2.0)

    assert result.ok is False
    assert "missing-model" in result.detail


def test_check_litellm_connection_error() -> None:
    settings = Settings(litellm_base_url="http://litellm.test:4000", litellm_api_key="sk-test")
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client.get.side_effect = httpx_connect_error()

    with patch("app.core.dependency_health.httpx.Client", return_value=mock_client):
        result = check_litellm(settings, timeout=1.0)
    assert result.ok is False
    assert "ConnectError" in result.detail or "error" in result.detail.lower() or result.detail


def httpx_connect_error():
    import httpx

    return httpx.ConnectError("connection refused")


def test_startup_strict_raises() -> None:
    settings = Settings(
        startup_dependency_check=True,
        startup_dependency_check_strict=True,
        litellm_base_url="http://127.0.0.1:1",
        parse_task_backend="local",
        layout_parser_name="simple",
    )
    # DB may pass; litellm will fail → strict raises
    bad = DependencyHealthReport(
        ok=False,
        checked_at=1.0,
        checks=[
            CheckResult(name="litellm", ok=False, required=True, detail="down"),
        ],
    )
    with patch(
        "app.core.dependency_health.run_dependency_checks", return_value=bad
    ), patch("app.core.dependency_health.log_dependency_report"):
        try:
            run_startup_dependency_checks(settings)
            raised = False
        except RuntimeError as exc:
            raised = True
            assert "strict" in str(exc).lower() or "failed" in str(exc).lower()
    assert raised is True


def test_startup_disabled_skips() -> None:
    settings = Settings(startup_dependency_check=False)
    report = run_startup_dependency_checks(settings)
    assert report.ok is True
    assert any(c.skipped for c in report.checks)


def test_run_dependency_checks_skips_valkey_and_mineru_when_unused() -> None:
    settings = Settings(
        parse_task_backend="local",
        layout_parser_name="simple",
        litellm_base_url="http://litellm.test:4000",
    )
    with patch(
        "app.core.dependency_health.check_database",
        return_value=CheckResult(name="database", ok=True, required=True, detail="ok"),
    ), patch(
        "app.core.dependency_health.check_litellm",
        return_value=CheckResult(name="litellm", ok=True, required=True, detail="ok"),
    ):
        report = run_dependency_checks(settings, timeout=1.0)
    names = {c.name: c for c in report.checks}
    assert names["valkey"].skipped is True
    assert names["mineru"].skipped is True
    assert report.ok is True
