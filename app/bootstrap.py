"""Runtime directory / engine bootstrap for lite (and full) deployments."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def sqlite_path_from_url(url: str) -> Path | None:
    """Return the filesystem path for a SQLite URL, or None for memory/non-file."""
    if not url.startswith("sqlite:"):
        return None
    # sqlite:///:memory:  |  sqlite://  |  sqlite:///relative  |  sqlite:////abs
    rest = url.removeprefix("sqlite:")
    if rest.startswith("////"):
        # Absolute path: sqlite:////data/db.sqlite -> /data/db.sqlite
        path_str = rest[3:]  # keep leading /
    elif rest.startswith("///"):
        path_str = rest[3:]
    else:
        return None
    if not path_str or path_str == ":memory:" or path_str.startswith(":"):
        return None
    return Path(path_str)


def ensure_runtime_dirs(settings: Settings | None = None) -> None:
    """Create local data directories (SQLite parent + local object store root)."""
    settings = settings or get_settings()
    if settings.storage_backend == "local":
        Path(settings.local_storage_path).mkdir(parents=True, exist_ok=True)
    db_path = sqlite_path_from_url(settings.database_url)
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)


def ensure_schema(bind: Engine | None = None) -> None:
    """Create missing tables from current SQLAlchemy models.

    Schema is still evolving; there is no migration history. ``create_all``
    only adds new tables — changing columns on an existing database requires
    wiping the SQLite file / volume.
    """
    from app.db.base import Base
    from app.models import Blob, CustomParseJob, FileReference, ParseResult  # noqa: F401

    if bind is None:
        from app.db.session import engine as bind
    Base.metadata.create_all(bind=bind)
    logger.info("database schema ensured (create_all; no migrations)")


def configure_sqlite_engine(engine: Engine) -> None:
    """Enable WAL + foreign keys + busy timeout for file-backed SQLite engines."""
    from app.config import get_settings

    busy_ms = max(0, get_settings().sqlite_busy_timeout_ms)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        if busy_ms > 0:
            cursor.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        cursor.close()
