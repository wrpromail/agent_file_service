from __future__ import annotations

from app.core.storage import get_storage
from app.db.session import SessionLocal
from app.services.lifecycle_service import expire_stale_references, reclaim_stuck_work, run_blob_gc
from app.tasks.celery_app import celery_app


@celery_app.task(name="gc.run_blob_gc")
def run_blob_gc_task() -> dict[str, int]:
    db = SessionLocal()
    storage = get_storage()
    try:
        count = run_blob_gc(db, storage)
        db.commit()
        return {"gc_count": count}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="gc.expire_references")
def expire_references_task() -> dict[str, int]:
    """Expire stale references AND run the stuck-work watchdog.

    C1 fix: reclaim_stuck_work used to only run inside run_blob_gc (daily interval).
    Running it here ensures stuck parse/custom-parse jobs are reclaimed on the
    more frequent expire interval (default: hourly) rather than waiting up to 24h.
    """
    db = SessionLocal()
    storage = get_storage()
    try:
        reclaim = reclaim_stuck_work(db, storage)
        count = expire_stale_references(db, storage)
        db.commit()
        return {"expired_count": count, **reclaim}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()