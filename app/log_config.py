"""Process log format for file-service API and Celery worker.

Line shape::

    YYYY-MM-DDTHH:MM:SS LEVEL [logger] message

Workers append ``process=ForkPoolWorker-N``. Compose healthchecks hit
``GET /health``; Prometheus (and the dev console) hit ``GET /metrics`` —
those access lines are dropped. Audit mutations stay on ``app.audit``.
"""

from __future__ import annotations

import logging
import logging.config
import os
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_FORMAT_WORKER = LOG_FORMAT + " process=%(processName)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"
ACCESS_FORMAT = (
    '%(asctime)s %(levelname)s [%(name)s] %(client_addr)s "%(request_line)s" %(status_code)s'
)

_QUIET_PATHS = frozenset({"/health", "/metrics"})


def is_worker_role() -> bool:
    return "worker" in (os.environ.get("SERVICE_ROLE") or "").lower()


def _access_path(record: logging.LogRecord) -> str:
    args = record.args
    if isinstance(args, dict):
        raw = args.get("path") or args.get("full_path") or args.get("request_line") or ""
        return str(raw)
    if isinstance(args, (tuple, list)):
        if len(args) >= 3 and isinstance(args[2], str) and args[2].startswith("/"):
            return args[2]
        for item in args:
            if isinstance(item, str) and item.startswith("/"):
                return item
    return record.getMessage()


class QuietHealthAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        raw = (_access_path(record) or "").strip()
        if " " in raw:
            parts = raw.split()
            raw = parts[1] if len(parts) >= 2 and parts[1].startswith("/") else raw
        path_only = raw.split("?", 1)[0].rstrip("/") or "/"
        return path_only not in _QUIET_PATHS


def _coerce_level(level: str | int | None = None, *, default: str = "INFO") -> str:
    if isinstance(level, int):
        name = logging.getLevelName(level)
        if isinstance(name, str) and name.isalpha():
            return name.upper()
        return default
    if isinstance(level, str) and level.strip():
        return level.strip().upper()
    raw = (
        os.environ.get("FILE_SERVICE_LOG_LEVEL")
        or os.environ.get("LOG_LEVEL")
        or default
    )
    return str(raw).strip().upper() or default


def logging_dict(*, level: str | int | None = None) -> dict[str, Any]:
    lvl = _coerce_level(level)
    message_format = LOG_FORMAT_WORKER if is_worker_role() else LOG_FORMAT
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "quiet_health": {
                "()": "app.log_config.QuietHealthAccessFilter",
            }
        },
        "formatters": {
            "standard": {
                "format": message_format,
                "datefmt": LOG_DATEFMT,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": ACCESS_FORMAT,
                "datefmt": LOG_DATEFMT,
                "use_colors": False,
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "filters": ["quiet_health"],
                "stream": "ext://sys.stderr",
            },
        },
        "root": {"level": lvl, "handlers": ["default"]},
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": lvl, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": lvl, "propagate": False},
            "uvicorn.access": {
                "handlers": ["access"],
                "level": lvl,
                "propagate": False,
            },
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "onnxruntime": {"level": "ERROR"},
        },
    }


def configure_logging(*, level: str | int | None = None) -> dict[str, Any]:
    cfg = logging_dict(level=level)
    logging.config.dictConfig(cfg)
    return cfg
