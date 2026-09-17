"""UC 会话闸：挂在 FastAPI 入口，开闸后无有效票且无服务 token → 401。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.uc.session import require_uc_session


class UcSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        denied = require_uc_session(request)
        if denied is not None:
            return denied
        return await call_next(request)
