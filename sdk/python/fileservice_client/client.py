"""
File Service Python SDK
=======================
轻量 httpx 客户端，供智能体后端 / MCP 工具调用 Agent File Service。
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Self

import httpx

from fileservice_client.errors import (
    FileServiceParseError,
    FileServiceTimeoutError,
    raise_for_status,
    safe_json,
)
from fileservice_client.models import ContentResult

__version__ = "0.1.0"

_DEFAULT_BASE_URL = "http://localhost:8020"
_FAILED_CODES = frozenset({"parse_failed", "parse_timeout", "parse_dependency_error"})


class FileServiceClient:
    """同步 httpx 客户端。进程默认实例：``init`` / ``get_client`` / ``from_env``。"""

    _lock = threading.Lock()
    _default: FileServiceClient | None = None

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        tenant_id: str | None = None,
        timeout: float = 60.0,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        default_headers: dict[str, str] | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._api_key = api_key if api_key is not None else os.getenv("FILE_SERVICE_API_KEY")
        self._tenant_id = tenant_id if tenant_id is not None else os.getenv("FILE_SERVICE_TENANT_ID")
        self._default_headers = dict(default_headers or {})
        self._client: httpx.Client | None = None

    @classmethod
    def init(
        cls,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        tenant_id: str | None = None,
        timeout: float = 60.0,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        default_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> FileServiceClient:
        extra = dict(kwargs)
        extra.setdefault("timeout", timeout)
        extra.setdefault("max_connections", max_connections)
        extra.setdefault("max_keepalive_connections", max_keepalive_connections)
        if api_key is not None:
            extra["api_key"] = api_key
        if tenant_id is not None:
            extra["tenant_id"] = tenant_id
        if default_headers is not None:
            extra["default_headers"] = default_headers
        client = cls(base_url, **extra) if base_url else cls.from_env(**extra)
        with cls._lock:
            cls._default = client
        return client

    @classmethod
    def get(cls) -> FileServiceClient:
        with cls._lock:
            if cls._default is None:
                cls._default = cls.from_env()
            return cls._default

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            previous = cls._default
            cls._default = None
        if previous is not None:
            previous.close()

    @classmethod
    def from_env(
        cls,
        *,
        base_url_env: str = "FILE_SERVICE_BASE_URL",
        api_key_env: str = "FILE_SERVICE_API_KEY",
        tenant_id_env: str = "FILE_SERVICE_TENANT_ID",
        default_base_url: str = _DEFAULT_BASE_URL,
        **kwargs: Any,
    ) -> FileServiceClient:
        base_url = os.getenv(base_url_env, default_base_url)
        if "api_key" not in kwargs:
            api_key = os.getenv(api_key_env)
            if api_key:
                kwargs["api_key"] = api_key
        if "tenant_id" not in kwargs:
            tenant_id = os.getenv(tenant_id_env)
            if tenant_id:
                kwargs["tenant_id"] = tenant_id
        return cls(base_url, **kwargs)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def tenant_id(self) -> str:
        return self._tenant_id or ""

    def set_tenant_id(self, tenant_id: str) -> FileServiceClient:
        self._tenant_id = tenant_id or ""
        return self

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(self._default_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self._tenant_id:
            headers["X-Tenant-Id"] = self._tenant_id
        if extra:
            headers.update(extra)
        return headers

    def start(self) -> None:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                limits=self._limits,
            )

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def _http(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self.start()
        assert self._client is not None
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        allow_304: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        resp = self._http.request(method, path, headers=self._headers(headers), **kwargs)
        if allow_304 and resp.status_code == 304:
            return resp
        raise_for_status(resp, context=path)
        return resp

    def health(self, *, deep: bool = False) -> dict[str, Any]:
        params = {"deep": "true"} if deep else None
        resp = self._http.get("/health", params=params)
        if resp.status_code not in (200, 503):
            raise_for_status(resp, context="/health")
        return safe_json(resp)

    def upload(
        self,
        file: str | Path | bytes | BinaryIO,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        uploaded_by: str | None = None,
        auto_parse: bool = True,
        metadata: dict[str, Any] | str | None = None,
        expires_at: str | None = None,
        archive_password: str | None = None,
    ) -> dict[str, Any]:
        close_after = False
        stream: BinaryIO
        name = filename
        if isinstance(file, (str, Path)):
            path = Path(file)
            name = name or path.name
            stream = path.open("rb")
            close_after = True
        elif isinstance(file, (bytes, bytearray)):
            if not name:
                raise TypeError("filename is required when uploading bytes")
            stream = io.BytesIO(bytes(file))
        else:
            stream = file
            if not name:
                name = getattr(file, "name", None)
                if isinstance(name, (str, Path)):
                    name = Path(name).name
                else:
                    name = None
            if not name:
                raise TypeError("filename is required when uploading a stream")
        data: dict[str, str] = {"auto_parse": "true" if auto_parse else "false"}
        if session_id is not None:
            data["session_id"] = session_id
        if agent_id is not None:
            data["agent_id"] = agent_id
        if uploaded_by is not None:
            data["uploaded_by"] = uploaded_by
        if metadata is not None:
            if isinstance(metadata, str):
                data["metadata"] = metadata
            else:
                data["metadata"] = json.dumps(metadata, ensure_ascii=False)
        if expires_at is not None:
            data["expires_at"] = expires_at
        if archive_password is not None:
            data["archive_password"] = archive_password
        files = {"file": (name, stream, content_type or "application/octet-stream")}
        try:
            resp = self._request("POST", "/v1/files", data=data, files=files)
        finally:
            if close_after:
                stream.close()
        return safe_json(resp)

    def from_url(
        self,
        url: str,
        *,
        filename: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        uploaded_by: str | None = None,
        auto_parse: bool = True,
        metadata: dict[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url, "auto_parse": auto_parse}
        if filename is not None:
            body["filename"] = filename
        if session_id is not None:
            body["session_id"] = session_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        if uploaded_by is not None:
            body["uploaded_by"] = uploaded_by
        if metadata:
            body["metadata"] = metadata
        if expires_at is not None:
            body["expires_at"] = expires_at
        resp = self._request("POST", "/v1/files/from-url", json=body)
        return safe_json(resp)

    def get_file(self, reference_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/v1/files/{reference_id}")
        return safe_json(resp)

    def list_files(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        uploaded_by: str | None = None,
        filename: str | None = None,
        content_hash: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if session_id is not None:
            params["session_id"] = session_id
        if agent_id is not None:
            params["agent_id"] = agent_id
        if uploaded_by is not None:
            params["uploaded_by"] = uploaded_by
        if filename is not None:
            params["filename"] = filename
        if content_hash is not None:
            params["content_hash"] = content_hash
        resp = self._request("GET", "/v1/files", params=params)
        return safe_json(resp)

    def delete(self, reference_id: str) -> None:
        self._request("DELETE", f"/v1/files/{reference_id}")

    def status(
        self,
        reference_id: str,
        *,
        profile: str | None = None,
        if_none_match: str | None = None,
    ) -> dict[str, Any] | None:
        params = {"profile": profile} if profile else None
        extra = {"If-None-Match": if_none_match} if if_none_match else None
        resp = self._request(
            "GET",
            f"/v1/files/{reference_id}/status",
            params=params,
            headers=extra,
            allow_304=True,
        )
        if resp.status_code == 304:
            return None
        return safe_json(resp)

    def content(
        self,
        reference_id: str,
        *,
        profile: str = "layout",
        smart_mode: bool = True,
        mode: str = "full",
        max_chars: int | None = None,
        offset: int = 0,
        sheets: str | None = None,
        pages: str | None = None,
        types: str | None = None,
        include_confidence: bool = True,
        if_none_match: str | None = None,
    ) -> ContentResult:
        params: dict[str, Any] = {
            "profile": profile,
            "smart_mode": str(smart_mode).lower(),
            "mode": mode,
            "offset": offset,
            "include_confidence": str(include_confidence).lower(),
        }
        if max_chars is not None:
            params["max_chars"] = max_chars
        if sheets is not None:
            params["sheets"] = sheets
        if pages is not None:
            params["pages"] = pages
        if types is not None:
            params["types"] = types
        extra = {"If-None-Match": if_none_match} if if_none_match else None
        resp = self._request(
            "GET",
            f"/v1/files/{reference_id}/content",
            params=params,
            headers=extra,
            allow_304=True,
        )
        etag = resp.headers.get("ETag")
        if resp.status_code == 304:
            return ContentResult(body={}, etag=etag, not_modified=True)
        return ContentResult(body=safe_json(resp), etag=etag, not_modified=False)

    def wait_for_content(
        self,
        reference_id: str,
        *,
        profile: str = "layout",
        timeout: float = 120.0,
        interval: float = 0.5,
        max_interval: float = 5.0,
        **content_kwargs: Any,
    ) -> ContentResult:
        """轮询 ``GET .../content``（ETag / 304）直到 ready 或失败。

        服务端不推送；本方法是智能体后端的等待封装。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        delay = max(0.05, float(interval))
        cap = max(delay, float(max_interval))
        etag: str | None = None
        last: ContentResult | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FileServiceTimeoutError(
                    f"等待 {reference_id} profile={profile} 超时（{timeout}s）",
                    body=(last.body if last else {}),
                )
            result = self.content(
                reference_id,
                profile=profile,
                if_none_match=etag,
                **content_kwargs,
            )
            if not result.not_modified:
                last = result
                if result.etag:
                    etag = result.etag
                if result.ready:
                    return result
                code = result.message_code or ""
                if result.status == "failed" or code in _FAILED_CODES:
                    raise FileServiceParseError(
                        result.message or f"解析失败 status={result.status} code={code}",
                        body=result.body,
                    )
            sleep_for = min(delay, deadline - time.monotonic())
            if sleep_for <= 0:
                raise FileServiceTimeoutError(
                    f"等待 {reference_id} profile={profile} 超时（{timeout}s）",
                    body=(last.body if last else {}),
                )
            time.sleep(sleep_for)
            delay = min(delay * 1.5, cap)

    def parse(
        self,
        reference_id: str,
        *,
        profile: str = "layout",
        force: bool = False,
    ) -> dict[str, Any]:
        resp = self._request(
            "POST",
            f"/v1/files/{reference_id}/parse",
            json={"profile": profile, "force": force},
        )
        return safe_json(resp)

    def custom_parse(
        self,
        reference_id: str,
        prompt: str,
        *,
        media_kind: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": prompt}
        if media_kind is not None:
            body["media_kind"] = media_kind
        if created_by is not None:
            body["created_by"] = created_by
        resp = self._request("POST", f"/v1/files/{reference_id}/custom-parse", json=body)
        return safe_json(resp)

    def list_custom_parses(
        self,
        reference_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        resp = self._request(
            "GET",
            f"/v1/files/{reference_id}/custom-parse",
            params={"limit": limit, "offset": offset},
        )
        return safe_json(resp)

    def get_custom_parse(self, reference_id: str, job_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/v1/files/{reference_id}/custom-parse/{job_id}")
        return safe_json(resp)

    def download(self, reference_id: str) -> bytes:
        resp = self._request("GET", f"/v1/files/{reference_id}/download")
        return resp.content

    def batch_status(
        self,
        reference_ids: list[str],
        *,
        profile: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"reference_ids": reference_ids}
        if profile is not None:
            body["profile"] = profile
        resp = self._request("POST", "/v1/files/batch-status", json=body)
        return safe_json(resp)

    def batch_content(
        self,
        reference_ids: list[str],
        *,
        profile: str = "layout",
        smart_mode: bool = True,
        mode: str = "full",
        max_chars: int | None = None,
        offset: int = 0,
        sheets: str | None = None,
        pages: str | None = None,
        types: str | None = None,
        include_confidence: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "reference_ids": reference_ids,
            "profile": profile,
            "smart_mode": smart_mode,
            "mode": mode,
            "offset": offset,
            "include_confidence": include_confidence,
        }
        if max_chars is not None:
            body["max_chars"] = max_chars
        if sheets is not None:
            body["sheets"] = sheets
        if pages is not None:
            body["pages"] = pages
        if types is not None:
            body["types"] = types
        resp = self._request("POST", "/v1/files/batch-content", json=body)
        return safe_json(resp)

    def convert_office(self, file_url: str, file_format: str) -> dict[str, Any]:
        """file_convert 兼容：Office → Markdown。"""
        resp = self._request(
            "POST",
            "/convert-office",
            json={"file_url": file_url, "file_format": file_format},
        )
        return safe_json(resp)

    def convert_markdown(self, markdown_content: str) -> dict[str, Any]:
        """file_convert 兼容：Markdown → PDF。"""
        resp = self._request("POST", "/convert", json={"markdown_content": markdown_content})
        return safe_json(resp)

    def extract_text(self, file_url: str, file_format: str) -> dict[str, Any]:
        """file_convert 兼容：抽取纯文本。"""
        resp = self._request(
            "POST",
            "/extract-text",
            json={"file_url": file_url, "file_format": file_format},
        )
        return safe_json(resp)

    def convert_to_images(
        self,
        file_url: str,
        file_format: str,
        *,
        image_format: str = "jpeg",
        quality: int = 80,
        max_width: int = 1800,
        dpi: int = 220,
        delivery_mode: str = "storage",
        pack_mode: str = "zip",
    ) -> dict[str, Any]:
        """file_convert 兼容：文档 → 页图。"""
        resp = self._request(
            "POST",
            "/convert-to-images",
            json={
                "file_url": file_url,
                "file_format": file_format,
                "image_format": image_format,
                "quality": quality,
                "max_width": max_width,
                "dpi": dpi,
                "delivery_mode": delivery_mode,
                "pack_mode": pack_mode,
            },
        )
        return safe_json(resp)

    def generate_excel(self, payload: dict[str, Any]) -> dict[str, Any]:
        """file_convert 兼容：JSON → xlsx。payload 为 files[] 或 source[]。"""
        resp = self._request("POST", "/generate-excel", json=payload)
        return safe_json(resp)


def init(
    base_url: str | None = None,
    *,
    api_key: str | None = None,
    tenant_id: str | None = None,
    **kwargs: Any,
) -> FileServiceClient:
    return FileServiceClient.init(base_url, api_key=api_key, tenant_id=tenant_id, **kwargs)


def get_client() -> FileServiceClient:
    return FileServiceClient.get()


def reset() -> None:
    FileServiceClient.reset()
