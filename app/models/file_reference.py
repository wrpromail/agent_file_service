from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.blob import Blob

from app.db.base import Base


class FileReference(Base):
    __tablename__ = "file_references"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    content_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("blobs.content_hash"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    custom_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    blob: Mapped["Blob"] = relationship(back_populates="references")