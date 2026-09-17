"""用户自定义提示词的媒体解析任务（图片 / 音频），与共享 parse_results 缓存隔离。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.blob import Blob
    from app.models.file_reference import FileReference

from app.db.base import Base


class CustomParseJob(Base):
    """按 reference + 用户 prompt 独立落库的自定义解析任务。

    与 ``parse_results``（按 content_hash 共享缓存）分离：同一文件不同 prompt
    各有一条记录，不互相覆盖。
    """

    __tablename__ = "custom_parse_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reference_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("file_references.id"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("blobs.content_hash"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    media_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # image | audio | video
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    parser_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # B2: tracks when status transitioned to 'processing'; used by watchdog for accurate timeout detection
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    blob: Mapped["Blob"] = relationship()
    reference: Mapped["FileReference"] = relationship()
