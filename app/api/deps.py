"""FastAPI dependencies for auth, DB, and storage.

鉴权约定（dev-security-standard §5.0）：

- 业务 API 走 Bearer（网关注入/透传后仍由本层校验）
- 开发控制台（``DEV_CONSOLE_ENABLED``）HTML + ``/shared/**`` 为免鉴权静态壳；浏览器 Bearer 来自 ``web/shared/config.js``（可用入口 ``DEV_CONSOLE_API_TOKEN`` 注入）
- 开闸后（``FILE_SERVICE_UC_ENABLED``）另验 ``X-UC-Session``：给人看的页面由前端闸；``/v1`` 无服务 token 须带本地票。有效 ``API_KEYS`` 机器调用豁免
- 多租户必填 ``X-Tenant-Id``
- 探活 /metrics 免鉴权；控制台关闭时不挂载静态壳；FastAPI /docs 等已在应用层关闭
"""
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.security import (
    AuthContext,
    api_key_id,
    api_key_is_allowed,
    assert_key_tenant_allowed,
    assert_tenant_allowed,
)
from app.core.storage import StorageBackend, get_storage
from app.db.session import get_db

AUTH_HEADER = "Authorization"
TENANT_HEADER = "X-Tenant-Id"


def extract_bearer_token(authorization: str | None) -> str:
    """Parse ``Authorization: Bearer <token>``; raise 401 on missing/invalid shape."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    return token


def verify_api_key(authorization: str | None, settings: Settings) -> str:
    """Require a valid Bearer API key. Returns the raw token. Auth cannot be disabled."""
    token = extract_bearer_token(authorization)
    allowed = settings.allowed_api_keys
    if not allowed or not api_key_is_allowed(token, allowed):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return token


def get_auth_context(
    authorization: Annotated[str | None, Header(alias=AUTH_HEADER)] = None,
    x_tenant_id: Annotated[str | None, Header(alias=TENANT_HEADER)] = None,
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    token = verify_api_key(authorization, settings)
    if not x_tenant_id or not x_tenant_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Tenant-Id header",
        )
    tenant_id = x_tenant_id.strip()
    assert_tenant_allowed(tenant_id, settings)
    assert_key_tenant_allowed(token, tenant_id, settings)
    return AuthContext(
        api_key=token,
        key_id=api_key_id(token),
        tenant_id=tenant_id,
    )


def get_current_tenant(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> str:
    return auth.tenant_id


DbSession = Annotated[Session, Depends(get_db)]
TenantId = Annotated[str, Depends(get_current_tenant)]
AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
StorageDep = Annotated[StorageBackend, Depends(get_storage)]
