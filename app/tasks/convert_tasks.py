"""Celery tasks for file_convert compatibility endpoints."""
from __future__ import annotations

import logging

from app.core.storage import get_storage
from app.db.session import SessionLocal
from app.services.convert_service import ConvertError, run_convert_op
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="convert.run")
def run_convert_task(op: str, **kwargs) -> dict:
    db = SessionLocal()
    storage = get_storage()
    try:
        result = run_convert_op(op, db, storage, **kwargs)
        db.commit()
        return result
    except ConvertError:
        db.rollback()
        logger.exception("convert op failed op=%s", op)
        raise
    except Exception:
        db.rollback()
        logger.exception("convert op failed op=%s", op)
        raise
    finally:
        db.close()
