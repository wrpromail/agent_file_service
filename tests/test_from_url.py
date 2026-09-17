from unittest.mock import patch

import httpx

from app.services.url_import_service import UrlImportError, validate_url_for_import
from tests.conftest import TEST_HEADERS


def test_ssrf_blocks_localhost():
    try:
        validate_url_for_import("http://127.0.0.1/secret")
        assert False, "expected block"
    except UrlImportError as exc:
        assert exc.code == "from_url_ssrf_blocked"


def test_ssrf_blocks_private():
    try:
        validate_url_for_import("http://192.168.1.1/x")
        assert False, "expected block"
    except UrlImportError as exc:
        assert exc.code == "from_url_ssrf_blocked"


def test_from_url_upload_mocked(client):
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
        # Use a public-looking host; DNS may still resolve — patch validate to skip DNS
        with patch(
            "app.services.url_import_service.validate_url_for_import",
            return_value="https://example.com/file.txt",
        ):
            resp = test_client.post(
                "/v1/files/from-url",
                headers=TEST_HEADERS,
                json={"url": "https://example.com/file.txt", "filename": "file.txt"},
            )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["metadata"]["filename"] == "file.txt"
