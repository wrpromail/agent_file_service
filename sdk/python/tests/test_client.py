from __future__ import annotations

import httpx
import pytest
import respx
from fileservice_client import (
    FileServiceAuthError,
    FileServiceClient,
    FileServiceNotFoundError,
    FileServiceParseError,
    FileServiceQuotaError,
    FileServiceTimeoutError,
    get_client,
    init,
    reset,
)

BASE = "http://files-test:8020"
REF = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _reset_default_client():
    yield
    reset()


@pytest.fixture
def fs() -> FileServiceClient:
    return FileServiceClient(BASE, api_key="dev-api-key", tenant_id="tenant-a", timeout=5.0)


class TestAuth:
    @respx.mock
    def test_sends_bearer_and_tenant(self, fs: FileServiceClient):
        route = respx.get(f"{BASE}/v1/files/{REF}").mock(
            return_value=httpx.Response(200, json={"reference_id": REF})
        )
        with fs:
            fs.get_file(REF)
        headers = route.calls.last.request.headers
        assert headers["Authorization"] == "Bearer dev-api-key"
        assert headers["X-Tenant-Id"] == "tenant-a"

    @respx.mock
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("FILE_SERVICE_BASE_URL", BASE)
        monkeypatch.setenv("FILE_SERVICE_API_KEY", "env-key")
        monkeypatch.setenv("FILE_SERVICE_TENANT_ID", "env-tenant")
        client = FileServiceClient.from_env()
        route = respx.get(f"{BASE}/v1/files/{REF}").mock(
            return_value=httpx.Response(200, json={"reference_id": REF})
        )
        with client:
            client.get_file(REF)
        assert route.calls.last.request.headers["Authorization"] == "Bearer env-key"
        assert route.calls.last.request.headers["X-Tenant-Id"] == "env-tenant"

    def test_init_get_client(self):
        init(BASE, api_key="k", tenant_id="t")
        assert get_client().base_url == BASE
        assert get_client().tenant_id == "t"


class TestErrors:
    @respx.mock
    def test_401(self, fs: FileServiceClient):
        respx.get(f"{BASE}/v1/files/{REF}").mock(
            return_value=httpx.Response(401, json={"detail": "unauthorized"})
        )
        with fs, pytest.raises(FileServiceAuthError):
            fs.get_file(REF)

    @respx.mock
    def test_404(self, fs: FileServiceClient):
        respx.get(f"{BASE}/v1/files/{REF}").mock(
            return_value=httpx.Response(404, json={"detail": "not found"})
        )
        with fs, pytest.raises(FileServiceNotFoundError):
            fs.get_file(REF)

    @respx.mock
    def test_429(self, fs: FileServiceClient):
        respx.post(f"{BASE}/v1/files/{REF}/parse").mock(
            return_value=httpx.Response(429, json={"detail": "quota"})
        )
        with fs, pytest.raises(FileServiceQuotaError):
            fs.parse(REF)


class TestConvertCompat:
    @respx.mock
    def test_convert_office(self, fs: FileServiceClient):
        route = respx.post(f"{BASE}/convert-office").mock(
            return_value=httpx.Response(
                200,
                json={
                    "markdown_content": "# x",
                    "task_id": "t",
                    "file_format": "docx",
                    "processing_time": 0.1,
                    "message": "ok",
                    "cache_hit": False,
                },
            )
        )
        with fs:
            data = fs.convert_office("http://example.com/a.docx", "docx")
        assert data["markdown_content"] == "# x"
        assert route.called


class TestUploadAndContent:
    @respx.mock
    def test_upload_bytes(self, fs: FileServiceClient):
        route = respx.post(f"{BASE}/v1/files").mock(
            return_value=httpx.Response(
                201,
                json={
                    "reference_id": REF,
                    "content_hash": "abc",
                    "deduplicated": False,
                    "parse_status": "pending",
                    "auto_parse_applied": True,
                },
            )
        )
        with fs:
            body = fs.upload(b"hello", filename="a.txt", session_id="s1", agent_id="ag")
        assert body["reference_id"] == REF
        req = route.calls.last.request
        assert b"filename" in req.content or "a.txt" in (req.headers.get("content-type") or "")
        assert b"s1" in req.content

    @respx.mock
    def test_content_ready(self, fs: FileServiceClient):
        respx.get(f"{BASE}/v1/files/{REF}/content").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ready": True,
                    "status": "completed",
                    "metadata": {"markdown": "# hi"},
                },
                headers={"ETag": '"etag1"'},
            )
        )
        with fs:
            result = fs.content(REF)
        assert result.ready
        assert result.markdown == "# hi"
        assert result.etag == '"etag1"'

    @respx.mock
    def test_content_304(self, fs: FileServiceClient):
        respx.get(f"{BASE}/v1/files/{REF}/content").mock(
            return_value=httpx.Response(304, headers={"ETag": '"etag1"'})
        )
        with fs:
            result = fs.content(REF, if_none_match='"etag1"')
        assert result.not_modified
        assert result.etag == '"etag1"'

    @respx.mock
    def test_status_304_returns_none(self, fs: FileServiceClient):
        respx.get(f"{BASE}/v1/files/{REF}/status").mock(
            return_value=httpx.Response(304, headers={"ETag": '"s1"'})
        )
        with fs:
            assert fs.status(REF, if_none_match='"s1"') is None


class TestWaitForContent:
    @respx.mock
    def test_wait_until_ready(self, fs: FileServiceClient, monkeypatch):
        monkeypatch.setattr("fileservice_client.client.time.sleep", lambda _s: None)
        pending = httpx.Response(
            200,
            json={"ready": False, "status": "processing", "message_code": "parse_processing"},
            headers={"ETag": '"e1"'},
        )
        done = httpx.Response(
            200,
            json={"ready": True, "status": "completed", "metadata": {"markdown": "ok"}},
            headers={"ETag": '"e2"'},
        )
        respx.get(f"{BASE}/v1/files/{REF}/content").mock(side_effect=[pending, done])
        with fs:
            result = fs.wait_for_content(REF, timeout=10, interval=0.01)
        assert result.ready
        assert result.markdown == "ok"

    @respx.mock
    def test_wait_failed_raises(self, fs: FileServiceClient, monkeypatch):
        monkeypatch.setattr("fileservice_client.client.time.sleep", lambda _s: None)
        respx.get(f"{BASE}/v1/files/{REF}/content").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ready": False,
                    "status": "failed",
                    "message": "模型不可达",
                    "message_code": "parse_dependency_error",
                },
            )
        )
        with fs, pytest.raises(FileServiceParseError, match="模型不可达"):
            fs.wait_for_content(REF, timeout=5, interval=0.01)

    @respx.mock
    def test_wait_timeout(self, fs: FileServiceClient, monkeypatch):
        monkeypatch.setattr("fileservice_client.client.time.sleep", lambda _s: None)
        clock = {"t": 0.0}

        def monotonic():
            clock["t"] += 10.0
            return clock["t"]

        monkeypatch.setattr("fileservice_client.client.time.monotonic", monotonic)
        respx.get(f"{BASE}/v1/files/{REF}/content").mock(
            return_value=httpx.Response(
                200,
                json={"ready": False, "status": "processing", "message_code": "parse_processing"},
            )
        )
        with fs, pytest.raises(FileServiceTimeoutError):
            fs.wait_for_content(REF, timeout=1, interval=0.01)


class TestOther:
    @respx.mock
    def test_parse_and_delete_and_batch(self, fs: FileServiceClient):
        respx.post(f"{BASE}/v1/files/{REF}/parse").mock(
            return_value=httpx.Response(202, json={"profile": "layout", "status": "pending"})
        )
        respx.delete(f"{BASE}/v1/files/{REF}").mock(return_value=httpx.Response(204))
        respx.post(f"{BASE}/v1/files/batch-status").mock(
            return_value=httpx.Response(200, json={"total": 1, "items": []})
        )
        respx.post(f"{BASE}/v1/files/batch-content").mock(
            return_value=httpx.Response(200, json={"total": 1, "items": []})
        )
        respx.post(f"{BASE}/v1/files/from-url").mock(
            return_value=httpx.Response(201, json={"reference_id": REF})
        )
        with fs:
            assert fs.parse(REF, profile="layout")["status"] == "pending"
            fs.delete(REF)
            assert fs.batch_status([REF])["total"] == 1
            assert fs.batch_content([REF])["total"] == 1
            assert fs.from_url("https://example.com/a.pdf")["reference_id"] == REF

    @respx.mock
    def test_download_and_custom_parse(self, fs: FileServiceClient):
        respx.get(f"{BASE}/v1/files/{REF}/download").mock(
            return_value=httpx.Response(200, content=b"PDF")
        )
        respx.post(f"{BASE}/v1/files/{REF}/custom-parse").mock(
            return_value=httpx.Response(202, json={"job_id": "j1", "status": "pending"})
        )
        with fs:
            assert fs.download(REF) == b"PDF"
            job = fs.custom_parse(REF, "读出发票金额")
            assert job["job_id"] == "j1"
