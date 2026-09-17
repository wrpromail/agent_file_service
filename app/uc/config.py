"""UC 工作台跳转配置 — 只认 env。

优先 ``FILE_SERVICE_UC_*``，同时接受 ``DASHBOARD_UC_*`` 别名。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "http://127.0.0.1"
DEFAULT_TTL_SEC = 28800
DEFAULT_TIMEOUT_MS = 10000
DEFAULT_TOKEN_QUERY = "token"
DEFAULT_USER_ID_CLAIM = "userCode"
DEFAULT_MOCK_USER_ID = "local-dev"


def env_raw(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def env_truthy(raw: str) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def ticket_secret() -> str:
    dedicated = env_raw(
        "FILE_SERVICE_UC_SESSION_SECRET",
        "DASHBOARD_UC_SESSION_SECRET",
    )
    if dedicated:
        return dedicated
    tokens = env_raw("API_KEYS", "API_TOKEN", "STATIC_TOKEN")
    if tokens:
        return tokens.split(",")[0].strip() or "uc-dev"
    try:
        from app.config import get_settings

        keys = (get_settings().api_keys or "").strip()
        if keys:
            return keys.split(",")[0].strip() or "uc-dev"
    except Exception:
        pass
    return "uc-dev"


def _timeout_ms(raw: str) -> int:
    try:
        timeout_sec = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    if not timeout_sec or timeout_sec <= 0:
        return DEFAULT_TIMEOUT_MS
    return int(timeout_sec if timeout_sec > 100 else timeout_sec * 1000)


def _ttl_sec(raw: str) -> int:
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SEC
    return ttl if ttl > 0 else DEFAULT_TTL_SEC


@dataclass(frozen=True)
class UcConfig:
    enabled: bool
    mock: bool
    base_url: str
    user_id_claim: str
    token_query: str
    mock_user_id: str
    app_code: str
    frontend_prefix: str
    backend_prefix: str
    timeout_ms: int
    session_ttl_sec: int
    ticket_secret: str


def get_uc_config() -> UcConfig:
    return UcConfig(
        enabled=env_truthy(env_raw("FILE_SERVICE_UC_ENABLED", "DASHBOARD_UC_ENABLED")),
        mock=env_truthy(env_raw("FILE_SERVICE_UC_MOCK", "DASHBOARD_UC_MOCK")),
        base_url=env_raw("FILE_SERVICE_UC_BASE_URL", "DASHBOARD_UC_BASE_URL")
        or DEFAULT_BASE_URL,
        user_id_claim=env_raw(
            "FILE_SERVICE_UC_USER_ID_CLAIM",
            "DASHBOARD_UC_USER_ID_CLAIM",
        )
        or DEFAULT_USER_ID_CLAIM,
        token_query=env_raw(
            "FILE_SERVICE_UC_TOKEN_QUERY",
            "DASHBOARD_UC_TOKEN_QUERY",
        )
        or DEFAULT_TOKEN_QUERY,
        mock_user_id=env_raw(
            "FILE_SERVICE_UC_MOCK_USER_ID",
            "DASHBOARD_UC_MOCK_USER_ID",
        )
        or DEFAULT_MOCK_USER_ID,
        app_code=env_raw("FILE_SERVICE_UC_APP_CODE", "DASHBOARD_UC_APP_CODE"),
        frontend_prefix=env_raw(
            "FILE_SERVICE_UC_FRONTEND_PREFIX",
            "DASHBOARD_UC_FRONTEND_PREFIX",
        ),
        backend_prefix=env_raw(
            "FILE_SERVICE_UC_BACKEND_PREFIX",
            "DASHBOARD_UC_BACKEND_PREFIX",
        ),
        timeout_ms=_timeout_ms(env_raw("FILE_SERVICE_UC_TIMEOUT", "DASHBOARD_UC_TIMEOUT")),
        session_ttl_sec=_ttl_sec(
            env_raw(
                "FILE_SERVICE_UC_SESSION_TTL_SECONDS",
                "DASHBOARD_UC_SESSION_TTL_SECONDS",
            )
        ),
        ticket_secret=ticket_secret(),
    )


def uc_status_payload() -> dict:
    cfg = get_uc_config()
    return {
        "enabled": cfg.enabled,
        "mock": cfg.mock,
        "token_query": cfg.token_query,
        "user_id_claim": cfg.user_id_claim,
        "app_code": cfg.app_code,
        "frontend_prefix": cfg.frontend_prefix,
        "backend_prefix": cfg.backend_prefix,
        "menus_ready": False,
    }
