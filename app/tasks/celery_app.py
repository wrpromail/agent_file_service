from celery import Celery
from celery.signals import setup_logging as on_celery_setup_logging
from celery.signals import worker_process_init

from app.config import get_settings
from app.log_config import configure_logging

settings = get_settings()

celery_app = Celery(
    "file_service",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.parse_tasks",
        "app.tasks.gc_tasks",
        "app.tasks.custom_parse_tasks",
    ],
)

# GC 定时由 API lifespan 驱动（见 app/main.py），不跑 Celery Beat。
_celery_conf: dict = {
    "task_serializer": "json",
    "accept_content": ["json"],
    "result_serializer": "json",
    "timezone": "UTC",
    "enable_utc": True,
    "task_always_eager": settings.parse_sync,
    "task_eager_propagates": True,
}
if settings.celery_task_time_limit > 0:
    _celery_conf["task_time_limit"] = settings.celery_task_time_limit
    _celery_conf["task_soft_time_limit"] = max(1, settings.celery_task_time_limit - 30)
celery_app.conf.update(**_celery_conf)


@on_celery_setup_logging.connect(dispatch_uid="file_service.setup_logging")
def _configure_celery_logging(loglevel=None, **kwargs) -> None:
    # Connected receiver → Celery skips its [ts: LEVEL/process] default format.
    configure_logging(level=loglevel)


@worker_process_init.connect(dispatch_uid="file_service.worker_init")
def _configure_worker_child(**kwargs) -> None:
    configure_logging()
    import logging

    logging.getLogger(__name__).info("file-service worker process initialized")
