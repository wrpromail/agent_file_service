"""图片 / 音频 / 视频用户自定义 prompt 解析（独立 custom_parse_jobs 表）。"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.storage import (
    StorageBackend,
    blob_storage_key,
    custom_parse_storage_key,
    get_storage,
    storage_key_from_uri,
)
from app.integrations.litellm.client import get_litellm_client
from app.models.blob import Blob
from app.models.custom_parse_job import CustomParseJob
from app.models.file_reference import FileReference
from app.schemas.files import (
    CustomParseJobListResponse,
    CustomParseJobResponse,
    CustomParseRequest,
)

SUPPORTED_CUSTOM_MEDIA = frozenset({"image", "audio", "video"})

_STATUS_MESSAGES: dict[str, tuple[str, str]] = {
    "pending": ("custom_parse_pending", "自定义解析排队中，请稍后再试。"),
    "processing": ("custom_parse_processing", "自定义解析进行中，请稍后再试。"),
    "completed": ("custom_parse_completed", "自定义解析已完成。"),
    "failed": ("custom_parse_failed", "自定义解析失败。"),
}


def infer_media_kind(*, mime_type: str, filename: str, explicit: str | None = None) -> str:
    if explicit:
        kind = explicit.strip().lower()
        if kind not in SUPPORTED_CUSTOM_MEDIA:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"media_kind must be one of: {', '.join(sorted(SUPPORTED_CUSTOM_MEDIA))}",
            )
        return kind
    mime = (mime_type or "").lower()
    name = (filename or "").lower()
    if mime.startswith("image/") or any(
        name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    ):
        return "image"
    if mime.startswith("audio/") or any(
        name.endswith(ext) for ext in (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".wma")
    ):
        return "audio"
    if mime.startswith("video/") or any(
        name.endswith(ext)
        for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", ".mpeg", ".mpg", ".m4v", ".3gp")
    ):
        return "video"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="仅支持图片、音频或视频文件的自定义解析；无法从 MIME/文件名推断 media_kind",
    )


def _parser_identity(media_kind: str, settings: Settings) -> tuple[str, str]:
    if media_kind == "image":
        return settings.vision_parser_name, settings.vision_parser_version
    if media_kind == "video":
        return settings.video_parser_name, settings.video_parser_version
    return settings.audio_parser_name, settings.audio_parser_version


def _job_to_response(job: CustomParseJob) -> CustomParseJobResponse:
    code, msg = _STATUS_MESSAGES.get(job.status, ("custom_parse_unknown", f"状态：{job.status}"))
    if job.status == "failed" and job.error:
        detail = job.error.strip()
        if len(detail) > 200:
            detail = detail[:200] + "…"
        msg = f"自定义解析失败：{detail}"
        code = "custom_parse_failed"
    return CustomParseJobResponse(
        job_id=job.id,
        reference_id=job.reference_id,
        content_hash=job.content_hash,
        media_kind=job.media_kind,
        prompt=job.prompt,
        status=job.status,
        parser_name=job.parser_name,
        parser_version=job.parser_version,
        result_text=job.result_text if job.status == "completed" else None,
        summary=job.summary or {},
        error=job.error,
        created_by=job.created_by,
        created_at=job.created_at,
        completed_at=job.completed_at,
        message=msg,
        message_code=code,
    )


def create_custom_parse_job(
    db: Session,
    reference: FileReference,
    body: CustomParseRequest,
    *,
    settings: Settings | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> CustomParseJob:
    settings = settings or get_settings()
    media_kind = infer_media_kind(
        mime_type=reference.blob.mime_type,
        filename=reference.original_filename,
        explicit=body.media_kind,
    )
    # 校验显式 media_kind 与文件一致
    inferred = infer_media_kind(
        mime_type=reference.blob.mime_type,
        filename=reference.original_filename,
        explicit=None,
    )
    if media_kind != inferred:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"media_kind={media_kind!r} 与文件类型 {inferred!r} 不匹配",
        )

    parser_name, parser_version = _parser_identity(media_kind, settings)
    job = CustomParseJob(
        reference_id=reference.id,
        content_hash=reference.content_hash,
        tenant_id=reference.tenant_id,
        media_kind=media_kind,
        prompt=body.prompt.strip(),
        status="pending",
        parser_name=parser_name,
        parser_version=parser_version,
        summary={},
        created_by=body.created_by,
    )
    db.add(job)
    db.flush()

    if settings.parse_sync:
        execute_custom_parse_job(db, job.id, settings=settings)
    else:
        _dispatch_async_custom_parse(job.id, settings, background_tasks)
    return job


def _enqueue_custom_parse_after_commit(job_id: uuid.UUID) -> None:
    from app.tasks.custom_parse_tasks import run_custom_parse_task

    run_custom_parse_task.delay(str(job_id))


def _dispatch_async_custom_parse(
    job_id: uuid.UUID,
    settings: Settings,
    background_tasks: BackgroundTasks | None,
) -> None:
    backend = (settings.parse_task_backend or "local").strip().lower()
    if backend == "celery":
        if background_tasks is not None:
            background_tasks.add_task(_enqueue_custom_parse_after_commit, job_id)
        else:
            _enqueue_custom_parse_after_commit(job_id)
        return

    if background_tasks is not None:
        background_tasks.add_task(run_background_custom_parse, job_id, settings)
    else:
        import threading

        thread = threading.Thread(
            target=run_background_custom_parse,
            args=(job_id, settings),
            daemon=True,
        )
        thread.start()


def run_background_custom_parse(job_id: uuid.UUID, settings: Settings) -> None:
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        execute_custom_parse_job(db, job_id, settings=settings)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def execute_custom_parse_job(
    db: Session,
    job_id: uuid.UUID,
    *,
    storage: StorageBackend | None = None,
    settings: Settings | None = None,
) -> CustomParseJob:
    settings = settings or get_settings()
    storage = storage or get_storage()

    job = db.get(CustomParseJob, job_id)
    if job is None:
        raise ValueError(f"CustomParseJob not found: {job_id}")

    from sqlalchemy import update

    claimed = db.execute(
        update(CustomParseJob)
        .where(
            CustomParseJob.id == job_id,
            CustomParseJob.status.in_(("pending", "failed")),
        )
        .values(status="processing", error=None, processing_started_at=datetime.now(timezone.utc))
    )
    if claimed.rowcount == 0:
        db.refresh(job)
        return job
    db.flush()
    db.refresh(job)

    blob = db.get(Blob, job.content_hash)
    if blob is None:
        job.status = "failed"
        job.error = "Blob not found"
        job.completed_at = datetime.now(timezone.utc)
        from app.core.metrics import inc_metric

        inc_metric("custom_parse_failed_total", media_kind=job.media_kind or "unknown")
        db.flush()
        return job

    try:
        key = blob_storage_key(blob.content_hash)
        # Soft guard: refuse absurd in-memory multimodal loads.
        max_bytes = 0
        if job.media_kind == "image":
            max_bytes = settings.auto_parse_image_max_bytes
        elif job.media_kind == "audio":
            max_bytes = settings.auto_parse_audio_max_bytes
        elif job.media_kind == "video":
            max_bytes = settings.auto_parse_video_max_bytes
        if max_bytes > 0 and blob.size_bytes > max_bytes:
            raise ValueError(
                f"{job.media_kind} size {blob.size_bytes} exceeds custom-parse limit {max_bytes}"
            )
        file_bytes = storage.get_object(key)
        client = get_litellm_client()
        filename = _filename_for_job(db, job)

        if job.media_kind == "image":
            result = client.analyze_image_with_prompt(
                file_bytes,
                mime_type=blob.mime_type,
                prompt=job.prompt,
                filename=filename,
            )
        elif job.media_kind == "audio":
            result = client.analyze_audio_with_prompt(
                file_bytes,
                mime_type=blob.mime_type,
                prompt=job.prompt,
                filename=filename,
            )
        elif job.media_kind == "video":
            result = client.analyze_video_with_prompt(
                file_bytes,
                mime_type=blob.mime_type,
                prompt=job.prompt,
                filename=filename,
            )
        else:
            raise ValueError(f"Unsupported media_kind: {job.media_kind}")

        text = (result.text or "").strip()
        payload = {
            "job_id": str(job.id),
            "reference_id": str(job.reference_id),
            "content_hash": job.content_hash,
            "media_kind": job.media_kind,
            "prompt": job.prompt,
            "text": text,
            "model": result.model,
            "parser_name": job.parser_name,
            "parser_version": job.parser_version,
        }
        result_key = custom_parse_storage_key(str(job.id))
        result_uri = storage.put_object(
            result_key,
            io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            "application/json",
        )

        job.status = "completed"
        job.result_uri = result_uri
        job.result_text = text
        job.summary = {
            "char_count": len(text),
            "model": result.model,
            "media_kind": job.media_kind,
            "filename": filename,
            "mime_type": blob.mime_type,
        }
        job.completed_at = datetime.now(timezone.utc)
        job.error = None
        from app.core.metrics import inc_metric

        inc_metric("custom_parse_completed_total", media_kind=job.media_kind or "unknown")
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:1000]
        job.completed_at = datetime.now(timezone.utc)
        from app.core.metrics import inc_metric

        inc_metric("custom_parse_failed_total", media_kind=job.media_kind or "unknown")
    db.flush()
    return job


def get_custom_parse_job(
    db: Session,
    *,
    tenant_id: str,
    reference_id: uuid.UUID,
    job_id: uuid.UUID,
) -> CustomParseJobResponse:
    job = db.scalar(
        select(CustomParseJob).where(
            CustomParseJob.id == job_id,
            CustomParseJob.reference_id == reference_id,
            CustomParseJob.tenant_id == tenant_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Custom parse job not found")
    return _job_to_response(job)


def list_custom_parse_jobs(
    db: Session,
    *,
    tenant_id: str,
    reference_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
) -> CustomParseJobListResponse:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    base = select(CustomParseJob).where(
        CustomParseJob.reference_id == reference_id,
        CustomParseJob.tenant_id == tenant_id,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(CustomParseJob.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return CustomParseJobListResponse(
        reference_id=reference_id,
        total=int(total),
        jobs=[_job_to_response(row) for row in rows],
    )


def load_custom_parse_payload(storage: StorageBackend, job: CustomParseJob) -> dict | None:
    if not job.result_uri:
        return None
    key = storage_key_from_uri(job.result_uri)
    raw = storage.get_object(key)
    return json.loads(raw.decode("utf-8"))


def _filename_for_job(db: Session, job: CustomParseJob) -> str:
    ref = db.get(FileReference, job.reference_id)
    if ref and ref.original_filename:
        return ref.original_filename
    return job.content_hash
