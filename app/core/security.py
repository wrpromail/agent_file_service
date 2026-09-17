"""Auth helpers and tenant-owned resource access checks.

对齐 ``dev-security-standard`` §5.0：

- 业务 API 鉴权永开（Bearer ``API_KEYS``）；空配置启动生成 ephemeral key
- 多租户：``X-Tenant-Id`` + 可选 ``ALLOWED_TENANT_IDS`` / ``API_KEY_TENANT_MAP``
- 网关可做入口鉴权，但**不能替代**本层校验（纵深防御）
- FastAPI ``/docs`` / ``/openapi.json`` 在 ``app.main`` 关闭
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.file_reference import FileReference

logger = logging.getLogger(__name__)

EPHEMERAL_KEY_WARNING = (
    "No API_KEYS configured. Generated ephemeral API key "
    "(restart will change it; set API_KEYS to pin across restarts). "
    "Auth cannot be disabled. Authorization: Bearer %s"
)


@dataclass(frozen=True)
class AuthContext:
    """Authenticated caller (never stores raw key in logs)."""

    api_key: str
    key_id: str
    tenant_id: str


def parse_api_keys(raw: str | None) -> set[str]:
    """Parse comma-separated API_KEYS into a set."""
    if not raw:
        return set()
    return {key.strip() for key in str(raw).split(",") if key.strip()}


def api_key_id(token: str) -> str:
    """Stable non-reversible short id for audit logs."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:12]


def ensure_api_keys(settings: Settings) -> str | None:
    """Ensure at least one API key is available at runtime.

    If ``settings.api_keys`` is empty/blank, generate an ephemeral key, write it
    back onto the settings instance, and log it at WARNING. Returns the
    generated key when created, otherwise ``None``.

    Auth cannot be disabled: an empty key list is never treated as open access.
    """
    existing = parse_api_keys(settings.api_keys)
    if existing:
        return None

    generated = secrets.token_urlsafe(32)
    # Mutate the cached Settings instance so subsequent requests see the key.
    settings.api_keys = generated
    logger.warning(EPHEMERAL_KEY_WARNING, generated)
    return generated


def api_key_is_allowed(token: str, allowed: set[str]) -> bool:
    """Constant-time-ish membership check against configured API keys."""
    if not token or not allowed:
        return False
    matched = False
    for key in allowed:
        if len(token) != len(key):
            continue
        # hmac.compare_digest is length-constant when lengths match.
        if hmac.compare_digest(token, key):
            matched = True
    return matched


# Backward-compatible alias (scheduler historically used token_in_keys)
token_in_keys = api_key_is_allowed


def assert_tenant_allowed(tenant_id: str, settings: Settings) -> None:
    """Raise 403 if a tenant allowlist is configured and tenant_id is outside it."""
    allowlist = settings.tenant_allowlist
    if allowlist is not None and tenant_id not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant not allowed",
        )


def assert_key_tenant_allowed(token: str, tenant_id: str, settings: Settings) -> None:
    """Raise 403 if API_KEY_TENANT_MAP binds the key and tenant is not allowed."""
    bindings = settings.api_key_tenant_bindings
    if not bindings:
        return
    # Only enforce when this key appears in the map; unbound keys fall back to global rules.
    if token not in bindings:
        # Spec: keys not in map keep old behavior (global allowlist only).
        return
    allowed = bindings[token]
    if allowed is None:
        return  # *
    if tenant_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key not allowed for this tenant",
        )


def get_owned_reference(
    db: Session,
    tenant_id: str,
    reference_id: uuid.UUID,
) -> FileReference:
    reference = db.scalar(
        select(FileReference).where(
            FileReference.id == reference_id,
            FileReference.deleted_at.is_(None),
        )
    )
    if reference is None or reference.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return reference
