"""file_convert 兼容 MCP（Streamable HTTP，挂载 /mcp）。须 Bearer + X-Tenant-Id。"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api.deps import get_auth_context
from app.config import get_settings
from app.core.security import AuthContext
from app.core.storage import get_storage
from app.db.session import SessionLocal
from app.services.convert_service import ConvertError, run_convert_op

logger = logging.getLogger(__name__)

_auth_ctx: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar(
    "convert_mcp_auth", default=None
)


class ConvertMcpAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path or ""
        if not path.startswith("/mcp"):
            return await call_next(request)
        try:
            ctx = get_auth_context(
                authorization=request.headers.get("authorization"),
                x_tenant_id=request.headers.get("x-tenant-id")
                or request.headers.get("X-Tenant-Id"),
                settings=get_settings(),
            )
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        token = _auth_ctx.set(ctx)
        try:
            return await call_next(request)
        finally:
            _auth_ctx.reset(token)


def _auth() -> AuthContext:
    ctx = _auth_ctx.get()
    if ctx is None:
        raise ConvertError("MCP tool requires Authorization and X-Tenant-Id", 401)
    return ctx


def _public_base() -> str:
    return (get_settings().convert_compat_public_base_url or "").rstrip("/")


def _call(op: str, **kwargs: Any) -> dict[str, Any]:
    auth = _auth()
    kwargs["tenant_id"] = auth.tenant_id
    kwargs.setdefault("public_base_url", _public_base())
    db = SessionLocal()
    storage = get_storage()
    try:
        result = run_convert_op(op, db, storage, **kwargs)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def build_mcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # pragma: no cover
        logger.warning("mcp package not installed; /mcp convert endpoint disabled")
        return None

    mcp = FastMCP("file-convert")

    @mcp.tool()
    def convert_office_to_markdown(file_url: str, file_format: str) -> str:
        """Convert Office documents to Markdown format.
        Supported formats: doc, docx, ppt, pptx, xls, xlsx
        """
        result = _call("office_to_markdown", file_url=file_url, file_format=file_format)
        return result["markdown_content"]

    @mcp.tool()
    def convert_markdown_to_pdf(markdown_content: str) -> str:
        """Convert Markdown content to PDF format. Returns base64-encoded PDF."""
        result = _call("markdown_to_pdf", markdown_content=markdown_content)
        return result.get("pdf_base64") or ""

    @mcp.tool()
    def extract_file_text(file_url: str, file_format: str) -> str:
        """Extract plain text. Formats: doc, docx, ppt, pptx, xls, xlsx, pdf."""
        result = _call("extract_text", file_url=file_url, file_format=file_format)
        return result["text_content"]

    @mcp.tool()
    def convert_document_to_images(
        file_url: str,
        file_format: str,
        image_format: str = "jpeg",
        quality: int = 80,
        max_width: int = 1800,
    ) -> str:
        """Convert document pages to images, returned as base64-encoded zip."""
        result = _call(
            "document_to_images",
            file_url=file_url,
            file_format=file_format,
            image_format=image_format,
            quality=quality,
            max_width=max_width,
            delivery_mode="base64",
            pack_mode="zip",
        )
        return result.get("archive_base64") or ""

    @mcp.tool()
    def generate_excel(sheets_json: str) -> str:
        """Generate Excel from JSON. Returns a download URL (auth required)."""
        config = json.loads(sheets_json)
        result = _call(
            "generate_excel",
            files=None,
            source=config.get("source"),
            file_name=config.get("file_name"),
        )
        return result.get("file_base64") or json.dumps(result.get("file_urls") or {}, ensure_ascii=False)

    @mcp.resource("file-convert://info")
    def service_info() -> str:
        return json.dumps(
            {
                "name": "file-convert",
                "version": "2.0.0",
                "tools": [
                    "convert_office_to_markdown",
                    "convert_markdown_to_pdf",
                    "extract_file_text",
                    "convert_document_to_images",
                    "generate_excel",
                ],
                "supported_formats": {
                    "office_to_markdown": ["doc", "docx", "ppt", "pptx", "xls", "xlsx"],
                    "markdown_to_pdf": ["markdown"],
                    "extract_text": ["doc", "docx", "ppt", "pptx", "xls", "xlsx", "pdf"],
                    "document_to_images": ["doc", "docx", "pdf"],
                    "generate_excel": ["json"],
                },
            }
        )

    return mcp


mcp = build_mcp()
