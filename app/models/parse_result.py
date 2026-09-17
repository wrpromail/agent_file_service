from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.blob import Blob

from app.db.base import Base


class ParseResult(Base):
    __tablename__ = "parse_results"
    __table_args__ = (
        UniqueConstraint(
            "content_hash",
            "parser_name",
            "parser_version",
            "profile",
            name="uq_parse_result_cache_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    content_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("blobs.content_hash"), nullable=False, index=True
    )
    parser_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    overall_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # B2: tracks when status transitioned to 'processing'; used by watchdog to detect stuck tasks
    # without this field, watchdog incorrectly uses created_at and may reclaim freshly-started tasks
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    blob: Mapped["Blob"] = relationship(back_populates="parse_results")