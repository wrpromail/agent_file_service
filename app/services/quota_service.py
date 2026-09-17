"""Per-tenant quotas for upload and concurrent parse work."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.metrics import inc_metric
from app.models.blob import Blob
from app.models.custom_parse_job import CustomParseJob
from app.models.file_reference import FileReference
from app.models.parse_result import ParseResult


@dataclass(frozen=True)
class QuotaSnapshot:
    file_count: int
    bytes_total: int
    concurrent_parses: int


class QuotaExceededError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _count_live_files(db: Session, tenant_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(FileReference)
            .where(
                FileReference.tenant_id == tenant_id,
                FileReference.deleted_at.is_(None),
            )
        )
        or 0
    )


def _sum_referenced_bytes(db: Session, tenant_id: str) -> int:
    """Sum blob sizes for live references (may double-count shared blobs — intentional Phase1)."""
    total = db.scalar(
        select(func.coalesce(func.sum(Blob.size_bytes), 0))
        .select_from(FileReference)
        .join(Blob, Blob.content_hash == FileReference.content_hash)
        .where(
            FileReference.tenant_id == tenant_id,
            FileReference.deleted_at.is_(None),
        )
    )
    return int(total or 0)


def _count_concurrent_parses(db: Session, tenant_id: str) -> int:
    parse_count = db.scalar(
        select(func.count())
        .select_from(ParseResult)
        .join(FileReference, FileReference.content_hash == ParseResult.content_hash)
        .where(
            FileReference.tenant_id == tenant_id,
            FileReference.deleted_at.is_(None),
            ParseResult.status.in_(("pending", "processing")),
            ParseResult.profile != "metadata",
        )
    )
    custom_count = db.scalar(
        select(func.count())
        .select_from(CustomParseJob)
        .join(FileReference, FileReference.id == CustomParseJob.reference_id)
        .where(
            FileReference.tenant_id == tenant_id,
            FileReference.deleted_at.is_(None),
            CustomParseJob.status.in_(("pending", "processing")),
        )
    )
    return int(parse_count or 0) + int(custom_count or 0)


def snapshot_quota(db: Session, tenant_id: str) -> QuotaSnapshot:
    return QuotaSnapshot(
        file_count=_count_live_files(db, tenant_id),
        bytes_total=_sum_referenced_bytes(db, tenant_id),
        concurrent_parses=_count_concurrent_parses(db, tenant_id),
    )


def check_upload_quota(
    db: Session,
    *,
    tenant_id: str,
    incoming_bytes: int = 0,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    snap = snapshot_quota(db, tenant_id)
    if settings.quota_max_files_per_tenant > 0 and snap.file_count >= settings.quota_max_files_per_tenant:
        raise QuotaExceededError(
            "quota_exceeded_files",
            f"Tenant file quota exceeded ({snap.file_count}/{settings.quota_max_files_per_tenant})",
        )
    if settings.quota_max_bytes_per_tenant > 0:
        projected = snap.bytes_total + max(0, int(incoming_bytes))
        if projected > settings.quota_max_bytes_per_tenant:
            raise QuotaExceededError(
                "quota_exceeded_bytes",
                f"Tenant storage quota exceeded ({projected}/{settings.quota_max_bytes_per_tenant} bytes)",
            )


def check_parse_quota(
    db: Session,
    *,
    tenant_id: str,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    if settings.quota_max_concurrent_parses_per_tenant <= 0:
        return
    concurrent = _count_concurrent_parses(db, tenant_id)
    if concurrent >= settings.quota_max_concurrent_parses_per_tenant:
        raise QuotaExceededError(
            "quota_exceeded_concurrent_parses",
            f"Tenant concurrent parse quota exceeded ({concurrent}/{settings.quota_max_concurrent_parses_per_tenant})",
        )


def raise_http_quota(exc: QuotaExceededError) -> None:
    inc_metric("quota_rejects_total", code=exc.code)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"message": exc.message, "message_code": exc.code},
    )
