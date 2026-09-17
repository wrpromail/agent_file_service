"""Dev control-plane static pages (skillmesh-style MPA)."""

from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.metrics import get_metrics
from app.main import _WEB_DIR, _mount_dev_console
from tests.conftest import TEST_HEADERS


def test_dashboard_html_served(client):
    test_client, _ = client
    r = test_client.get("/dashboard.html")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert b"Agent File Service" in r.content or b"pageRoot" in r.content


def test_index_serves_meta_refresh_to_dashboard(client):
    """index.html is FileResponse 200 with client-side jump (not HTTP 3xx)."""
    test_client, _ = client
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    body = r.content.lower()
    assert b"dashboard" in body
    assert b"refresh" in body or b"location.replace" in body


def test_shared_static_assets(client):
    test_client, _ = client
    for path in (
        "/shared/styles/tokens.css",
        "/shared/styles/app.css",
        "/shared/js/chrome.js",
        "/shared/config.js",
    ):
        r = test_client.get(path)
        assert r.status_code == 200, path


def test_static_tree_mount_removed(client):
    """Whole-tree /static was redundant with /shared; must not re-expose web/."""
    test_client, _ = client
    r = test_client.get("/static/dashboard.html")
    assert r.status_code == 404
    r2 = test_client.get("/static/shared/config.js")
    assert r2.status_code == 404


def test_html_pages_no_auth_required(client):
    test_client, _ = client
    for path in (
        "/files.html",
        "/file.html",
        "/upload.html",
        "/session.html",
        "/metrics.html",
        "/playground.html",
        "/auth",
    ):
        r = test_client.get(path)
        assert r.status_code == 200, path


def test_shared_config_no_auth_but_v1_requires_bearer(client):
    test_client, _ = client
    cfg = test_client.get("/shared/config.js")
    assert cfg.status_code == 200
    assert b"apiToken" in cfg.content
    unauth = test_client.get("/v1/files")
    assert unauth.status_code == 401


def test_dev_console_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("DEV_CONSOLE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        application = FastAPI()
        _mount_dev_console(application, _WEB_DIR)
        with TestClient(application) as tc:
            assert tc.get("/dashboard.html").status_code == 404
            assert tc.get("/shared/config.js").status_code == 404
    finally:
        get_settings.cache_clear()


def test_metrics_include_http_counter(client):
    get_metrics().reset()
    test_client, _ = client
    test_client.get("/health")
    r = test_client.get("/metrics")
    assert r.status_code == 200
    assert "http_requests_total" in r.text


def test_http_metric_counts_server_errors(client):
    """Route failures must appear as 5xx (response path or exception path)."""
    get_metrics().reset()
    test_client, _ = client

    def boom() -> None:
        raise RuntimeError("metric-probe")

    test_client.app.add_api_route(
        "/__metric_probe_boom",
        boom,
        methods=["GET"],
        include_in_schema=False,
    )
    # FastAPI turns unhandled errors into 500 responses; TestClient may or may not re-raise.
    try:
        r = test_client.get("/__metric_probe_boom")
        assert r.status_code == 500
    except RuntimeError as exc:
        assert "metric-probe" in str(exc)
    m = test_client.get("/metrics")
    assert 'status_class="5xx"' in m.text
    assert "http_requests_total" in m.text


def test_content_read_metric(client):
    import io

    get_metrics().reset()
    test_client, _ = client
    up = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("m.txt", io.BytesIO(b"hello metrics content"), "text/plain")},
        data={"auto_parse": "true"},
    )
    assert up.status_code == 201
    ref = up.json()["reference_id"]
    c = test_client.get(f"/v1/files/{ref}/content?profile=layout", headers=TEST_HEADERS)
    assert c.status_code == 200
    m = test_client.get("/metrics")
    assert "content_reads_total" in m.text


def test_from_url_metric_ok_and_client_error(client):
    get_metrics().reset()
    test_client, _ = client
    payload = b"hello from url"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs["follow_redirects"] = False
        return real_client(*args, **kwargs)

    with patch("httpx.Client", side_effect=client_factory):
        with patch(
            "app.services.url_import_service.validate_url_for_import",
            return_value="https://example.com/file.txt",
        ):
            ok = test_client.post(
                "/v1/files/from-url",
                headers=TEST_HEADERS,
                json={"url": "https://example.com/file.txt", "filename": "file.txt"},
            )
    assert ok.status_code == 201, ok.text
    metrics = get_metrics()
    assert metrics.get("from_url_total", result="ok") >= 1
    # from_url ⊂ uploads
    assert metrics.get("uploads_total") >= 1

    get_metrics().reset()
    bad = test_client.post(
        "/v1/files/from-url",
        headers=TEST_HEADERS,
        json={"url": "http://127.0.0.1/secret"},
    )
    assert bad.status_code == 400
    assert get_metrics().get("from_url_total", result="client_error") >= 1
    assert get_metrics().get("from_url_total", result="error") == 0


def test_download_and_delete_metrics(client):
    import io

    get_metrics().reset()
    test_client, _ = client
    up = test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("d.txt", io.BytesIO(b"download-me"), "text/plain")},
    )
    assert up.status_code == 201
    ref = up.json()["reference_id"]
    dl = test_client.get(f"/v1/files/{ref}/download", headers=TEST_HEADERS)
    assert dl.status_code == 200
    assert get_metrics().get("downloads_total") >= 1
    rm = test_client.delete(f"/v1/files/{ref}", headers=TEST_HEADERS)
    assert rm.status_code == 204
    assert get_metrics().get("deletes_total") >= 1
