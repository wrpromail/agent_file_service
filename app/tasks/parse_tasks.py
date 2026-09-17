from __future__ import annotations

import logging
import uuid

from app.db.session import SessionLocal
from app.services.parse_service import execute_parse
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="parse.run_layout")
def run_layout_parse_task(parse_result_id: str) -> dict[str, str]:
    """Execute a pending parse_result (layout / deep / …) on the worker."""
    logger.info("parse started parse_result_id=%s", parse_result_id)
    db = SessionLocal()
    try:
        row = execute_parse(db, uuid.UUID(parse_result_id))
        db.commit()
        logger.info(
            "parse finished parse_result_id=%s content_hash=%s profile=%s status=%s",
            parse_result_id,
            row.content_hash,
            row.profile,
            row.status,
        )
        return {"parse_result_id": parse_result_id, "status": row.status}
    except Exception:
        db.rollback()
        logger.exception("parse failed parse_result_id=%s", parse_result_id)
        raise
    finally:
        db.close()
