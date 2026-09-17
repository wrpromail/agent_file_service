import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FileMetadata(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int
    page_count: int | None = None
    sheet_count: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ParseResultSummary(BaseModel):
    profile: str
    status: str
    parser_name: str | None = None
    overall_confidence: float | None = None


class ArchiveChildSummary(BaseModel):
    """压缩包展开后的子文件句柄（供 Agent 多附件）。"""

    reference_id: uuid.UUID
    filename: str
    content_hash: str
    deduplicated: bool = False
    parse_status: str = "not_started"
    metadata: FileMetadata | None = None


class ArchiveExpandInfo(BaseModel):
    """上传响应中的压缩包处理结果；非压缩包时为 null。"""

    status: str  # expanded | too_many | empty | password_required | failed | disabled
    format: str | None = None
    eligible_count: int = 0
    skipped_count: int = 0
    max_expand_files: int = 8
    message: str | None = None
    encrypted: bool = False
    children: list[ArchiveChildSummary] = Field(default_factory=list)


class UploadResponse(BaseModel):
    reference_id: uuid.UUID
    content_hash: str
    deduplicated: bool
    metadata: FileMetadata
    parse_status: str
    expires_at: datetime | None = None
    # 自动 layout→Markdown 门闸（分类型开关 + 门槛）
    auto_parse_requested: bool = True
    auto_parse_applied: bool = False
    auto_parse_skip_reason: str | None = None
    auto_parse_message: str | None = None
    # 压缩包展开信息；普通文件为 null
    archive: ArchiveExpandInfo | None = None


class FileDetailResponse(BaseModel):
    reference_id: uuid.UUID
    content_hash: str
    tenant_id: str
    session_id: str | None
    agent_id: str | None
    expires_at: datetime | None = None
    metadata: FileMetadata
    custom_metadata: dict[str, Any]
    parse_results: list[ParseResultSummary]


class FileStatusResponse(BaseModel):
    reference_id: uuid.UUID
    metadata: FileMetadata
    parse_results: list[ParseResultSummary]


class ParseRequest(BaseModel):
    profile: str = "layout"
    force: bool = False


class ParseTriggerResponse(BaseModel):
    profile: str
    status: str
    parse_result_id: uuid.UUID | None = None


# ── Custom parse (image / audio / video + user prompt) ───────────────────────


class CustomParseRequest(BaseModel):
    """用户自定义提示词解析图片、音频或视频。"""

    prompt: str = Field(..., min_length=1, max_length=8000, description="用户对识别/转写/分析的要求")
    media_kind: str | None = Field(
        default=None,
        description="image | audio | video；省略则按文件 MIME/扩展名推断",
    )
    created_by: str | None = None


class CustomParseJobResponse(BaseModel):
    job_id: uuid.UUID
    reference_id: uuid.UUID
    content_hash: str
    media_kind: str
    prompt: str
    status: str
    parser_name: str
    parser_version: str
    result_text: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_by: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    message: str | None = None
    message_code: str | None = None


class CustomParseJobListResponse(BaseModel):
    reference_id: uuid.UUID
    total: int
    jobs: list[CustomParseJobResponse]


class FileReferenceSummary(BaseModel):
    reference_id: uuid.UUID
    content_hash: str
    original_filename: str
    metadata: FileMetadata
    created_at: datetime
    expires_at: datetime | None = None


class SessionFilesResponse(BaseModel):
    session_id: str
    files: list[FileReferenceSummary]


# ── Functional 1: file list ──────────────────────────────────────────────────

class FileListItem(BaseModel):
    reference_id: uuid.UUID
    content_hash: str
    original_filename: str
    session_id: str | None
    agent_id: str | None
    uploaded_by: str | None
    custom_metadata: dict[str, Any]
    created_at: datetime
    expires_at: datetime | None = None
    metadata: FileMetadata


class FileListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    files: list[FileListItem]


# ── Functional 2: reference clone ────────────────────────────────────────────

class CloneRequest(BaseModel):
    target_tenant_id: str
    session_id: str | None = None
    agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class CloneResponse(BaseModel):
    reference_id: uuid.UUID
    source_reference_id: uuid.UUID
    content_hash: str
    target_tenant_id: str
    deduplicated: bool
    metadata: FileMetadata


# ── Phase1: PATCH reference ─────────────────────────────────────────────────


class ReferenceUpdateRequest(BaseModel):
    """Partial update of FileReference (blob untouched).

    Omitted fields are left unchanged. Explicit JSON null clears nullable
    fields (session_id / agent_id / uploaded_by / expires_at).
    """

    session_id: str | None = None
    agent_id: str | None = None
    uploaded_by: str | None = None
    custom_metadata: dict[str, Any] | None = None
    metadata_mode: str = Field(
        default="merge",
        description="merge = shallow merge into existing custom_metadata; replace = overwrite",
    )
    expires_at: datetime | None = None

    model_config = {"extra": "forbid"}


# ── Phase1: batch ───────────────────────────────────────────────────────────


class BatchStatusRequest(BaseModel):
    reference_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=100)
    profile: str | None = None


class BatchStatusItem(BaseModel):
    reference_id: uuid.UUID
    ok: bool = True
    error: str | None = None
    error_code: str | None = None
    status: FileStatusResponse | None = None


class BatchStatusResponse(BaseModel):
    total: int
    items: list[BatchStatusItem]


class BatchContentRequest(BaseModel):
    reference_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=100)
    profile: str = "layout"
    smart_mode: bool = True
    mode: str = "full"
    max_chars: int | None = None
    offset: int = 0
    sheets: str | None = None
    pages: str | None = None
    types: str | None = None
    include_confidence: bool = True


class BatchContentItem(BaseModel):
    reference_id: uuid.UUID
    ok: bool = True
    error: str | None = None
    error_code: str | None = None
    content: dict[str, Any] | None = None


class BatchContentResponse(BaseModel):
    total: int
    items: list[BatchContentItem]


# ── Phase1: from-url ────────────────────────────────────────────────────────


class FromUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=4096)
    filename: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    uploaded_by: str | None = None
    auto_parse: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


# ── Phase1: archive members ─────────────────────────────────────────────────


class ArchiveMemberItem(BaseModel):
    path: str
    size: int
    eligible: bool = True
    skip_reason: str | None = None


class ArchiveMembersResponse(BaseModel):
    reference_id: uuid.UUID
    format: str | None = None
    status: str
    encrypted: bool = False
    max_expand_files: int = 8
    eligible_count: int = 0
    skipped_count: int = 0
    message: str | None = None
    members: list[ArchiveMemberItem] = Field(default_factory=list)