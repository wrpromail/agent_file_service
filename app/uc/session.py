"""UC 换票 + 进程内 jti 单会话互斥 + 闸门。

无用户表：user_id → 当前 jti 存在内存；进程重启后须重新从工作台进入。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Request
from fastapi.responses import JSONResponse

from app.uc.client import UcError, strip_token, token_auth
from app.uc.config import UcConfig, get_uc_config, uc_status_payload
from app.uc.ticket import HEADER_UC_SESSION, issue_ticket, parse_ticket, verify_ticket

logger = logging.getLogger(__name__)

USER_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
MOCK_USER_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")
CLAIM_ALIASES = {
    "userCode": ["userCode", "usreCode", "user_code", "code"],
    "username": ["username"],
    "id": ["id"],
}

_current_jti_by_user: dict[str, str] = {}


class UcExchangeError(Exception):
    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


class TokenAuthClient(Protocol):
    def token_auth(self, token: str, *, base_url: str, timeout_ms: int = 10000) -> dict: ...


@dataclass(frozen=True)
class UcPrincipal:
    user_id: str
    display_name: str
    username: str
    tenant_id: str
    disabled: bool


@dataclass(frozen=True)
class UcSessionResult:
    user_id: str | None
    error: str | None


def first_str(data: dict, keys: list[str]) -> str:
    for key in keys:
        val = data.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            return s
    return ""


def normalize_user_id(raw: str | None) -> str:
    s = str(raw or "").strip()
    if not s or not USER_ID_RE.match(s):
        return ""
    return s


def map_uc_principal(auth: Any, user_id_claim: str) -> UcPrincipal:
    blob = auth if isinstance(auth, dict) else {}
    claim = str(user_id_claim or "userCode").strip() or "userCode"
    aliases = CLAIM_ALIASES.get(claim, [claim])
    raw_id = first_str(blob, aliases)
    if not raw_id:
        raw_id = first_str(blob, CLAIM_ALIASES["userCode"])
    if not raw_id:
        raw_id = first_str(blob, CLAIM_ALIASES["username"])
    user_id = normalize_user_id(raw_id)
    if not user_id:
        raise ValueError("uc principal has no usable user_id")

    username = first_str(blob, ["username"])
    real_name = first_str(blob, ["realName", "nickname"])
    tenant_id = first_str(blob, ["tenantId", "tenant_id"])
    status = blob.get("status")
    return UcPrincipal(
        user_id=user_id,
        display_name=real_name or username or user_id,
        username=username or "",
        tenant_id=tenant_id or "",
        disabled=status == 2 or status == "2",
    )


def mock_token_auth(raw_token: str, cfg: UcConfig) -> dict:
    uid = str(cfg.mock_user_id or "local-dev").strip() or "local-dev"
    token = str(raw_token or "").strip()
    if token and "." not in token and MOCK_USER_RE.match(token):
        uid = token
    return {
        "userCode": uid,
        "username": uid,
        "tenantId": "local-mock",
    }


def remember_jti(user_id: str, jti: str) -> None:
    _current_jti_by_user[str(user_id)] = str(jti)


def lookup_jti(user_id: str) -> str | None:
    return _current_jti_by_user.get(str(user_id))


def token_probe(token: str) -> str:
    n = len(str(token or ""))
    if n >= 16:
        return f"len={n} fp={token[:3]}..{token[-3:]} jwtish={int('.' in token)}"
    return f"len={n} jwtish={int('.' in str(token or ''))}"


def exchange_uc_token(raw_token: str, *, client: TokenAuthClient | None = None) -> dict:
    cfg = get_uc_config()
    if not cfg.enabled:
        raise UcExchangeError("uc sso is disabled", status_code=503)
    token = strip_token(raw_token)
    if not token:
        raise UcExchangeError("token required", status_code=400)

    use_mock = bool(cfg.mock) and client is None
    logger.info(
        "uc exchange start mock=%s uc_base=%s %s",
        int(use_mock),
        cfg.base_url or "-",
        token_probe(token),
    )

    if use_mock:
        logger.warning("uc mock exchange (FILE_SERVICE_UC_MOCK=1); not calling live UC")
        auth = mock_token_auth(token, cfg)
    else:
        try:
            if client is not None:
                auth = client.token_auth(token, base_url=cfg.base_url, timeout_ms=cfg.timeout_ms)
            else:
                auth = token_auth(token, base_url=cfg.base_url, timeout_ms=cfg.timeout_ms)
        except UcError as err:
            logger.warning(
                "uc token-auth failed status=%s detail=%s %s",
                err.status_code,
                err,
                token_probe(token),
            )
            raise UcExchangeError(str(err), status_code=err.status_code) from err
        except Exception as err:
            logger.warning("uc token-auth failed status=502 detail=%s %s", err, token_probe(token))
            raise UcExchangeError(str(err) or "uc token-auth failed", status_code=502) from err

    try:
        principal = map_uc_principal(auth, cfg.user_id_claim)
    except ValueError as err:
        raise UcExchangeError(str(err) or "uc principal has no usable user_id", status_code=401) from err
    if principal.disabled:
        raise UcExchangeError("uc user is disabled", status_code=403)

    session_ticket = issue_ticket(
        principal.user_id,
        secret=cfg.ticket_secret,
        ttl_sec=cfg.session_ttl_sec,
    )
    parsed = parse_ticket(session_ticket)
    if parsed is None:
        raise UcExchangeError("failed to issue session ticket", status_code=500)
    remember_jti(principal.user_id, parsed.jti)

    source = "uc_mock" if use_mock else "uc"
    logger.info(
        "uc exchange ok source=%s user_id=%s ttl_sec=%s",
        source,
        principal.user_id,
        cfg.session_ttl_sec,
    )
    return {
        "user_id": principal.user_id,
        "display_name": principal.display_name,
        "username": principal.username,
        "role": "user",
        "tenant_id": principal.tenant_id,
        "source": source,
        "persisted": True,
        "menu_codes": [],
        "session_ticket": session_ticket,
        "warning": None,
    }


def _norm_path(pathname: str) -> str:
    p = str(pathname or "").rstrip("/") or "/"
    return p


def is_uc_auth_path(method: str, pathname: str) -> str:
    m = str(method or "GET").upper()
    p = _norm_path(pathname)
    if m == "GET" and p.endswith("/auth/uc/status"):
        return "status"
    if m == "POST" and p.endswith("/auth/uc/exchange"):
        return "exchange"
    return ""


def is_uc_session_exempt(method: str, pathname: str) -> bool:
    """status / exchange / liveness / metrics / 静态壳。机器 Bearer 在闸门里另判。"""
    m = str(method or "GET").upper()
    p = _norm_path(pathname)
    raw = str(pathname or "")
    if is_uc_auth_path(m, pathname):
        return True
    if m == "OPTIONS":
        return True
    if m == "GET" and p == "/health":
        return True
    if m == "GET" and p == "/metrics":
        return True
    if raw.startswith("/shared"):
        return True
    if p in {"/", "/auth"} or raw.endswith(".html"):
        return True
    return False


def resolve_uc_session(ticket: str | None, cfg: UcConfig | None = None) -> UcSessionResult:
    cfg = cfg or get_uc_config()
    uid = verify_ticket(ticket, secret=cfg.ticket_secret) if ticket else None
    if not uid:
        return UcSessionResult(
            user_id=None,
            error="uc session invalid or expired" if ticket else "uc session required",
        )
    parsed = parse_ticket(ticket)
    jti = parsed.jti if parsed else ""
    current = lookup_jti(uid)
    if not jti or not current or current != jti:
        return UcSessionResult(user_id=None, error="uc session invalid or expired")
    return UcSessionResult(user_id=uid, error=None)


def _has_valid_service_token(request: Request) -> bool:
    """已有 L0 服务票（API_KEYS）的机器 / webhook 豁免 UC。"""
    from app.config import get_settings
    from app.core.security import api_key_is_allowed

    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    token = auth.removeprefix("Bearer ").strip()
    if not token:
        return False
    return api_key_is_allowed(token, get_settings().allowed_api_keys)


def require_uc_session(request: Request) -> JSONResponse | None:
    """开闸后拦无票请求。返回 JSON 401 或 None（放行）。"""
    cfg = get_uc_config()
    if not cfg.enabled:
        return None
    if is_uc_session_exempt(request.method, request.url.path):
        return None
    if _has_valid_service_token(request):
        return None

    ticket = (request.headers.get(HEADER_UC_SESSION) or "").strip()
    result = resolve_uc_session(ticket, cfg)
    if result.user_id:
        request.state.uc_user_id = result.user_id
        return None
    return JSONResponse(
        status_code=401,
        content={"detail": result.error},
    )


def reset_uc_session_for_tests() -> None:
    _current_jti_by_user.clear()


__all__ = [
    "UcExchangeError",
    "UcPrincipal",
    "UcSessionResult",
    "exchange_uc_token",
    "is_uc_auth_path",
    "is_uc_session_exempt",
    "lookup_jti",
    "map_uc_principal",
    "mock_token_auth",
    "remember_jti",
    "require_uc_session",
    "reset_uc_session_for_tests",
    "resolve_uc_session",
    "uc_status_payload",
]
