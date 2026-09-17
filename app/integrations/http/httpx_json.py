"""基于 httpx 的 JSON 出站请求（带重试）。供 LiteLLM 等使用。"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from app.integrations.http.retry import (
    PermanentHttpError,
    RetryableError,
    RetryPolicy,
    call_with_retry,
)

_shared_client: httpx.Client | None = None


def get_shared_httpx_client(timeout: float = 60.0) -> httpx.Client:
    """Process-wide httpx client with connection pooling (LiteLLM etc.)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.Client(timeout=timeout)
    return _shared_client


def close_shared_httpx_client() -> None:
    global _shared_client
    if _shared_client is not None:
        _shared_client.close()
        _shared_client = None


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float = 60.0,
    policy: RetryPolicy | None = None,
    error_prefix: str = "HTTP",
    sleep: Callable[[float], None] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """发送 JSON 请求并解析响应体为 dict。

    对 429 / 5xx / 超时 / 连接错误按 ``policy`` 重试；其它 4xx 立即失败。
    未传入 ``client`` 时创建短生命周期客户端（便于单测 mock）；生产路径由
    LiteLLMClient 注入共享 client。
    """
    policy = policy or RetryPolicy()
    sleep_fn = sleep if sleep is not None else __import__("time").sleep

    def _once() -> dict[str, Any]:
        owns_client = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            try:
                response = http.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=json,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                raise RetryableError(
                    f"{error_prefix} timeout: {exc}",
                    cause=exc,
                ) from exc
            except httpx.TransportError as exc:
                raise RetryableError(
                    f"{error_prefix} transport error: {exc}",
                    cause=exc,
                ) from exc

            status = response.status_code
            body_preview = (response.text or "")[:2000]

            if policy.is_retryable_status(status):
                raise RetryableError(
                    f"{error_prefix} HTTP {status}: {body_preview}",
                    status_code=status,
                    rate_limited=(status == 429),
                )

            if status >= 400:
                raise PermanentHttpError(
                    f"{error_prefix} HTTP {status}: {body_preview}",
                    status_code=status,
                    body=body_preview,
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise PermanentHttpError(
                    f"{error_prefix} invalid JSON response: {body_preview[:500]}",
                    status_code=status,
                    body=body_preview,
                    cause=exc,
                ) from exc
            if not isinstance(data, dict):
                raise PermanentHttpError(
                    f"{error_prefix} expected JSON object, got {type(data).__name__}",
                    status_code=status,
                )
            return data
        finally:
            if owns_client:
                http.close()

    try:
        return call_with_retry(_once, policy=policy, sleep=sleep_fn)
    except RetryableError as exc:
        raise RuntimeError(
            f"{error_prefix} request failed after {policy.max_attempts} attempt(s): {exc}"
        ) from exc
    except PermanentHttpError as exc:
        raise RuntimeError(str(exc)) from exc
