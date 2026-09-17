"""Local filesystem storage backend (lite mode default)."""

from io import BytesIO
from pathlib import Path

import pytest

from app.config import Settings
from app.core.storage import (
    LocalFilesystemStorageBackend,
    create_storage,
    storage_key_from_uri,
)


@pytest.fixture()
def local_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture()
def local_storage(local_root: Path) -> LocalFilesystemStorageBackend:
    settings = Settings(
        storage_backend="local",
        local_storage_path=str(local_root),
    )
    return LocalFilesystemStorageBackend(settings)


def test_put_get_delete_roundtrip(local_storage: LocalFilesystemStorageBackend, local_root: Path):
    key = "blobs/ab/abcd1234"
    uri = local_storage.put_object(key, BytesIO(b"hello-lite"), "text/plain")
    assert uri == f"local://{key}"
    assert storage_key_from_uri(uri) == key
    assert local_storage.exists(key)
    assert (local_root / "blobs" / "ab" / "abcd1234").read_bytes() == b"hello-lite"
    assert local_storage.get_object(key) == b"hello-lite"

    buf = BytesIO()
    local_storage.stream_object(key, buf)
    assert buf.getvalue() == b"hello-lite"

    local_storage.delete_object(key)
    assert not local_storage.exists(key)


def test_rejects_path_traversal(local_storage: LocalFilesystemStorageBackend):
    with pytest.raises(ValueError, match="invalid storage key"):
        local_storage.put_object("../etc/passwd", BytesIO(b"x"), "text/plain")


def test_create_storage_local(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(tmp_path / "data"))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        backend = create_storage(get_settings())
        assert isinstance(backend, LocalFilesystemStorageBackend)
        uri = backend.put_object("blobs/aa/a", BytesIO(b"z"), "application/octet-stream")
        assert uri.startswith("local://")
    finally:
        get_settings.cache_clear()


def test_sqlite_path_and_bootstrap(tmp_path: Path):
    from app.bootstrap import ensure_runtime_dirs, sqlite_path_from_url
    from app.config import Settings

    db = tmp_path / "nested" / "db" / "fs.db"
    settings = Settings(
        database_url=f"sqlite:///{db}",
        storage_backend="local",
        local_storage_path=str(tmp_path / "blobs-root"),
    )
    assert sqlite_path_from_url(settings.database_url) == db
    ensure_runtime_dirs(settings)
    assert db.parent.is_dir()
    assert (tmp_path / "blobs-root").is_dir()


def test_ensure_schema_creates_core_tables(tmp_path: Path):
    from sqlalchemy import create_engine, inspect

    from app.bootstrap import ensure_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'fs.db'}")
    ensure_schema(bind=engine)
    names = set(inspect(engine).get_table_names())
    assert names >= {"blobs", "file_references", "parse_results", "custom_parse_jobs"}


def test_load_structured_content_local_uri(local_storage: LocalFilesystemStorageBackend):
    """Regression: lite mode stores parse results as local:// URIs; content load must strip scheme."""
    import json
    from types import SimpleNamespace

    from app.services.parse_service import load_structured_content

    payload = {
        "schema_version": "1.0",
        "content_hash": "ab" * 32,
        "source": {
            "parser_name": "markitdown",
            "parser_version": "0.1.0",
            "profile": "layout",
        },
        "document": {"page_count": 1, "language": []},
        "blocks": [
            {
                "id": "b1",
                "type": "paragraph",
                "page": 1,
                "text": "lite-local-content",
                "confidence": 1.0,
            }
        ],
        "tables": [],
        "warnings": [],
        "metadata": {"markdown": "lite-local-content", "format": "markdown"},
    }
    key = "parse-results/ab/abcd/markitdown/layout.json"
    uri = local_storage.put_object(
        key,
        BytesIO(json.dumps(payload).encode("utf-8")),
        "application/json",
    )
    assert uri == f"local://{key}"

    # Minimal stand-in for ParseResult (only result_uri is read)
    row = SimpleNamespace(result_uri=uri)
    content = load_structured_content(local_storage, row)  # type: ignore[arg-type]
    assert content.metadata.get("markdown") == "lite-local-content"
    assert content.blocks[0].text == "lite-local-content"

    # Passing the raw URI as a key must NOT work (documents the bug we fixed)
    with pytest.raises((FileNotFoundError, OSError, ValueError)):
        local_storage.get_object(uri)
