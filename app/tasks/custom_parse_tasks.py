from __future__ import annotations

import logging
import uuid

from app.db.session import SessionLocal
from app.services.custom_parse_service import execute_custom_parse_job
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="parse.run_custom")
def run_custom_parse_task(job_id: str) -> dict[str, str]:
    """Execute a pending custom_parse_jobs row on the worker."""
    logger.info("custom parse started job_id=%s", job_id)
    db = SessionLocal()
    try:
        job = execute_custom_parse_job(db, uuid.UUID(job_id))
        db.commit()
        logger.info(
            "custom parse finished job_id=%s tenant_id=%s content_hash=%s media_kind=%s status=%s",
            job_id,
            job.tenant_id,
            job.content_hash,
            job.media_kind,
            job.status,
        )
        return {"job_id": job_id, "status": job.status}
    except Exception:
        db.rollback()
        logger.exception("custom parse failed job_id=%s", job_id)
        raise
    finally:
        db.close()
