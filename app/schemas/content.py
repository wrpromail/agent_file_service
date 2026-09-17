from typing import Any, Literal

from pydantic import BaseModel, Field


class ContentSource(BaseModel):
    parser_name: str
    parser_version: str
    profile: str


class DocumentInfo(BaseModel):
    page_count: int | None = None
    language: list[str] = Field(default_factory=list)


class ContentBlock(BaseModel):
    id: str
    type: Literal["heading", "paragraph", "table", "image", "list", "footer"]
    page: int
    bbox: list[float] | None = None
    text: str | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentTable(BaseModel):
    id: str
    page: int
    bbox: list[float] | None = None
    rows: list[list[str]] = Field(default_factory=list)
    confidence: float | None = None
    cell_confidences: list[list[float | None]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentWarning(BaseModel):
    block_id: str | None = None
    type: Literal["ocr_low_confidence", "engine_disagreement", "parse_partial"]
    detail: str
    candidates: list[str] = Field(default_factory=list)


class StructuredContent(BaseModel):
    """统一 content 响应（含就绪态 envelope）。

    - ``ready=true``：正文可用（blocks / metadata.markdown）。
    - ``ready=false``：解析未完成或失败；HTTP 仍为 200，用 ``message`` /
      ``message_code`` 做产品文案，**不再用 409 表示未就绪**。
    """

    schema_version: str = "1.0"
    content_hash: str
    source: ContentSource
    document: DocumentInfo = Field(default_factory=DocumentInfo)
    blocks: list[ContentBlock] = Field(default_factory=list)
    tables: list[ContentTable] = Field(default_factory=list)
    warnings: list[ContentWarning] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 读路径 envelope（方案 B）：未就绪也 200
    ready: bool = True
    status: str = "completed"
    message: str | None = None
    message_code: str | None = None


# parse status → (message_code, user-facing message)
_PARSE_STATUS_MESSAGES: dict[str, tuple[str, str]] = {
    "not_started": (
        "parse_not_started",
        "尚未开始内容识别。可稍后重试，或触发 layout 解析。",
    ),
    "pending": (
        "parse_pending",
        "内容识别排队中，请稍后再试。",
    ),
    "processing": (
        "parse_processing",
        "内容识别进行中，请稍后再试。",
    ),
    "failed": (
        "parse_failed",
        "内容识别失败，请重试解析。",
    ),
}


def classify_parse_error(error: str | None) -> str:
    """Map free-text parse error to a stable message_code."""
    if not error:
        return "parse_failed"
    err_l = error.lower()
    if any(tok in err_l for tok in ("timeout", "timed out", "reclaimed: stuck")):
        return "parse_timeout"
    if any(tok in err_l for tok in ("litellm", "mineru", "connection refused", "503", "502", "dns")):
        return "parse_dependency_error"
    if "unsupported" in err_l or "no parser" in err_l:
        return "parse_unsupported_profile"
    return "parse_failed"


def resolve_content_message(
    status: str,
    *,
    error: str | None = None,
    message_code_hint: str | None = None,
) -> tuple[str, str]:
    """Map parse status to (message_code, message)."""
    key = (status or "not_started").strip().lower()
    if key == "failed":
        code = message_code_hint or classify_parse_error(error)
        if error:
            detail = error.strip()
            if len(detail) > 200:
                detail = detail[:200] + "…"
            return code, f"内容识别失败：{detail}"
        return code, _PARSE_STATUS_MESSAGES["failed"][1]
    if key in _PARSE_STATUS_MESSAGES:
        return _PARSE_STATUS_MESSAGES[key]
    return (
        "parse_not_ready",
        f"内容识别尚未就绪（状态：{status or 'unknown'}），请稍后再试。",
    )


def build_not_ready_content(
    *,
    content_hash: str,
    profile: str,
    parser_name: str,
    parser_version: str,
    status: str,
    error: str | None = None,
    filename: str | None = None,
    mime_type: str | None = None,
) -> StructuredContent:
    """Soft envelope when parse result is not completed (HTTP 200)."""
    message_code, message = resolve_content_message(status, error=error)
    meta: dict[str, Any] = {
        "ready": False,
        "markdown": None,
        "status": status,
        "message": message,
        "message_code": message_code,
        "profile": profile,
        "retryable": status in {"pending", "processing", "failed", "not_started"},
    }
    if filename:
        meta["filename"] = filename
    if mime_type:
        meta["mime_type"] = mime_type
    if error:
        meta["error"] = error
    return StructuredContent(
        content_hash=content_hash,
        source=ContentSource(
            parser_name=parser_name,
            parser_version=parser_version,
            profile=profile,
        ),
        document=DocumentInfo(),
        blocks=[],
        tables=[],
        warnings=[],
        metadata=meta,
        ready=False,
        status=status,
        message=message,
        message_code=message_code,
    )


def mark_content_ready(content: StructuredContent, *, status: str = "completed") -> StructuredContent:
    """Stamp ready envelope on a successful structured payload."""
    meta = dict(content.metadata or {})
    meta.setdefault("ready", True)
    meta["status"] = status
    return content.model_copy(
        update={
            "ready": True,
            "status": status,
            "message": None,
            "message_code": None,
            "metadata": meta,
        }
    )