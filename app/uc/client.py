"""UC 平台 HTTP 客户端。本期只调 token-auth。

信封：``{ code: 0|"0", message, data }``
"""

from __future__ import annotations

from typing import Any

import httpx


class UcError(Exception):
    def __init__(self, message: str, *, status_code: int = 502, uc_code: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.uc_code = uc_code


def strip_token(raw: str | None) -> str:
    value = str(raw or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def parse_uc_envelope(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise UcError("uc response is not an object", status_code=502)
    code = payload.get("code")
    ok = code == 0 or code == "0"
    if not ok:
        msg = str(payload.get("message") or "uc request failed")
        http_status = 401
        try:
            st = int(payload.get("status"))
        except (TypeError, ValueError):
            st = 0
        if st >= 500:
            http_status = 502
        raise UcError(msg, status_code=http_status, uc_code=code)
    return payload.get("data")


def as_object(data: Any, what: str) -> dict:
    if not isinstance(data, dict):
        raise UcError(f"{what} data missing", status_code=401)
    return data


def token_auth(token: str, *, base_url: str, timeout_ms: int = 10000) -> dict:
    root = str(base_url or "").strip().rstrip("/")
    if not root:
        raise UcError("uc base_url is empty", status_code=503)
    url = f"{root}/uc/api/login/token-auth"
    timeout_s = max(int(timeout_ms or 10000), 1) / 1000.0
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.get(
                url,
                params={"token": strip_token(token)},
                headers={"Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise UcError("uc request timed out", status_code=504) from exc
    except httpx.HTTPError as exc:
        raise UcError(f"uc unreachable: {exc}", status_code=502) from exc

    try:
        payload = resp.json()
    except Exception as exc:
        raise UcError(f"uc non-json response (HTTP {resp.status_code})", status_code=502) from exc
    return as_object(parse_uc_envelope(payload), "token-auth")
