import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.core.storage import InMemoryStorageBackend, set_storage
from app.db.base import Base
from app.db.session import get_db
from app.integrations.litellm.client import set_litellm_client
from app.integrations.mineru.client import set_mineru_client
from app.main import app
from app.models import Blob, CustomParseJob, FileReference, ParseResult  # noqa: F401 — register metadata
from app.parsers.factory import set_processing_factory
from app.parsers.registry import set_registry
from app.uc.session import reset_uc_session_for_tests

TEST_API_KEY = "dev-api-key"
TEST_HEADERS = {
    "Authorization": f"Bearer {TEST_API_KEY}",
    "X-Tenant-Id": "tenant-a",
}


@pytest.fixture(autouse=True)
def configure_test_env(monkeypatch):
    monkeypatch.setenv("PARSE_SYNC", "true")
    monkeypatch.setenv("LAYOUT_PARSER_NAME", "simple")
    monkeypatch.setenv("VALKEY_URL", "redis://localhost:6379/15")
    # 固定测试 API key，避免依赖空默认 + 启动自动生成
    monkeypatch.setenv("API_KEYS", TEST_API_KEY)
    # 默认不限制 tenant，个别用例再覆盖 ALLOWED_TENANT_IDS
    monkeypatch.setenv("ALLOWED_TENANT_IDS", "")
    # 单元测试默认关闭图片/音频/视频自动多模态，避免误调外部 LiteLLM；专用用例再显式打开
    monkeypatch.setenv("AUTO_PARSE_IMAGE_ENABLED", "false")
    monkeypatch.setenv("AUTO_PARSE_AUDIO_ENABLED", "false")
    monkeypatch.setenv("AUTO_PARSE_VIDEO_ENABLED", "false")
    # 避免 TestClient lifespan 启动时真实探测 LiteLLM / Valkey
    monkeypatch.setenv("STARTUP_DEPENDENCY_CHECK", "false")
    # 默认关 UC 闸，避免既有业务用例被拦；UC 用例在 test_uc_auth 里自行开闸
    monkeypatch.setenv("FILE_SERVICE_UC_ENABLED", "0")
    monkeypatch.setenv("FILE_SERVICE_UC_MOCK", "0")
    get_settings.cache_clear()
    set_registry(None)
    set_processing_factory(None)
    set_mineru_client(None)
    set_litellm_client(None)
    reset_uc_session_for_tests()
    yield
    reset_uc_session_for_tests()
    get_settings.cache_clear()
    set_registry(None)
    set_processing_factory(None)
    set_mineru_client(None)
    set_litellm_client(None)


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(db_engine):
    session_factory = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_engine):
    session_factory = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    storage = InMemoryStorageBackend()
    set_storage(storage)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client, storage

    app.dependency_overrides.clear()
    set_storage(None)


@pytest.fixture()
def auth_headers():
    return TEST_HEADERS.copy()


@pytest.fixture()
def other_tenant_headers(auth_headers):
    return {**auth_headers, "X-Tenant-Id": "tenant-b"}


def reload_parsers(monkeypatch=None, **env: str) -> None:
    """Apply optional env overrides and reset settings / parser singletons."""
    if monkeypatch is not None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    set_processing_factory(None)
    set_registry(None)


def install_fake_markitdown(monkeypatch, *, text: str = "", title: str | None = None, convert=None):
    """Install a fake ``markitdown.MarkItDown`` into ``sys.modules``."""

    class FakeMarkItDown:
        def convert(self, path: str):
            if convert is not None:
                return convert(path)
            return SimpleNamespace(text_content=text, title=title)

    monkeypatch.setitem(sys.modules, "markitdown", SimpleNamespace(MarkItDown=FakeMarkItDown))
    return FakeMarkItDown


def upload_bytes(
    client,
    headers,
    payload: bytes,
    *,
    name: str = "f.txt",
    mime: str = "text/plain",
    **form,
):
    """POST /v1/files with the given bytes. ``client`` is the ``(TestClient, storage)`` fixture."""
    test_client, _ = client
    data = form or None
    return test_client.post(
        "/v1/files",
        headers=headers,
        files={"file": (name, payload, mime)},
        data=data,
    )
