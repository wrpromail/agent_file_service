from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bootstrap import configure_sqlite_engine, ensure_runtime_dirs, sqlite_path_from_url
from app.config import get_settings

settings = get_settings()
ensure_runtime_dirs(settings)


def _build_engine():
    url = settings.database_url
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        # In-memory SQLite needs a single shared connection across threads/sessions.
        if sqlite_path_from_url(url) is None and (":memory:" in url or url.rstrip("/") == "sqlite:"):
            engine = create_engine(
                url,
                connect_args=connect_args,
                poolclass=StaticPool,
            )
        else:
            engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        configure_sqlite_engine(engine)
        return engine
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
