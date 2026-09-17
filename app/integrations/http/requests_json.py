"""基于 requests 的 JSON 出站请求（带重试）。供 MinerU 等使用。"""
from __future__ import annotations

from typing import Any, Callable

import requests

from app.integrations.http.retry import (
    PermanentHttpError,
    RetryableError,
    RetryPolicy,
    call_with_retry,
)


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float = 120.0,
    policy: RetryPolicy | None = None,
    error_prefix: str = "HTTP",
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """使用已有 ``requests.Session`` 发 JSON 请求；策略与 httpx 版一致。"""
    policy = policy or RetryPolicy()
    sleep_fn = sleep if sleep is not None else __import__("time").sleep

    def _once() -> dict[str, Any]:
        try:
            response = session.request(
                method=method.upper(),
                url=url,
                json=json,
                headers=headers,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise RetryableError(f"{error_prefix} timeout: {exc}", cause=exc) from exc
        except requests.exceptions.ConnectionError as exc:
            raise RetryableError(f"{error_prefix} connection error: {exc}", cause=exc) from exc
        except requests.exceptions.RequestException as exc:
            raise RetryableError(f"{error_prefix} request error: {exc}", cause=exc) from exc

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

    try:
        return call_with_retry(_once, policy=policy, sleep=sleep_fn)
    except RetryableError as exc:
        raise RuntimeError(
            f"{error_prefix} request failed after {policy.max_attempts} attempt(s): {exc}"
        ) from exc
    except PermanentHttpError as exc:
        raise RuntimeError(str(exc)) from exc
