from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.file_reference import FileReference
    from app.models.parse_result import ParseResult


# Lifecycle states for blob GC coordination.
#   idle    — normal, available for reuse / GC
#   purging — GC has claimed the blob and is deleting its storage + parse results.
#             Uploads hitting a purging blob must wait until the row settles, then
#             either reuse a still-existing row (if ref_count>0 survives) or
#             re-create storage for a GC_PURGED_URI row.
GC_STATE_IDLE = "idle"
GC_STATE_PURGING = "purging"


class Blob(Base):
    __tablename__ = "blobs"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gc_state: Mapped[str] = mapped_column(String(16), nullable=False, default=GC_STATE_IDLE, server_default=GC_STATE_IDLE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    references: Mapped[list["FileReference"]] = relationship(back_populates="blob")
    parse_results: Mapped[list["ParseResult"]] = relationship(back_populates="blob")