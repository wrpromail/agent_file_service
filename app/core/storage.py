from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.client import Config

from app.config import Settings, get_settings

# Path used by derivative_storage_key


class StorageBackend(ABC):
    @abstractmethod
    def put_object(self, key: str, body: BinaryIO, content_type: str) -> str:
        """Store object and return storage URI."""

    @abstractmethod
    def get_object(self, key: str) -> bytes:
        """Fetch object bytes by key."""

    @abstractmethod
    def stream_object(self, key: str, dest: BinaryIO, chunk_size: int = 1024 * 1024) -> None:
        """Stream object content into ``dest`` (a writable binary file-like)."""

    def iter_object(self, key: str, chunk_size: int = 1024 * 1024):
        """Yield object bytes in chunks (default implementation via stream_object)."""
        import tempfile

        with tempfile.SpooledTemporaryFile(max_size=chunk_size, mode="w+b") as tmp:
            self.stream_object(key, tmp, chunk_size=chunk_size)
            tmp.seek(0)
            while True:
                chunk = tmp.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    @abstractmethod
    def delete_object(self, key: str) -> None:
        """Delete object by key."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check whether object exists."""


class LocalFilesystemStorageBackend(StorageBackend):
    """Content-addressed blobs on local disk (default for lite mode)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._root = Path(self._settings.local_storage_path).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str) -> Path:
        # Normalize and reject path traversal outside the storage root.
        cleaned = key.lstrip("/").replace("\\", "/")
        if ".." in cleaned.split("/"):
            raise ValueError(f"invalid storage key: {key!r}")
        path = (self._root / cleaned).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"invalid storage key: {key!r}") from exc
        return path

    def _uri(self, key: str) -> str:
        return f"local://{key}"

    def put_object(self, key: str, body: BinaryIO, content_type: str) -> str:
        path = self._path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write: stream to temp sibling then replace.
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(body, out, length=1024 * 1024)
            tmp.replace(path)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        return self._uri(key)

    def get_object(self, key: str) -> bytes:
        return self._path_for_key(key).read_bytes()

    def stream_object(self, key: str, dest: BinaryIO, chunk_size: int = 1024 * 1024) -> None:
        path = self._path_for_key(key)
        with open(path, "rb") as src:
            shutil.copyfileobj(src, dest, length=chunk_size)

    def iter_object(self, key: str, chunk_size: int = 1024 * 1024):
        path = self._path_for_key(key)
        with open(path, "rb") as src:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def delete_object(self, key: str) -> None:
        path = self._path_for_key(key)
        path.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path_for_key(key).is_file()


class S3StorageBackend(StorageBackend):
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint_url,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            region_name=self._settings.s3_region,
            use_ssl=self._settings.s3_use_ssl,
            config=Config(signature_version="s3v4"),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        bucket = self._settings.s3_bucket
        try:
            self._client.head_bucket(Bucket=bucket)
        except self._client.exceptions.ClientError:
            self._client.create_bucket(Bucket=bucket)

    def _uri(self, key: str) -> str:
        return f"s3://{self._settings.s3_bucket}/{key}"

    def put_object(self, key: str, body: BinaryIO, content_type: str) -> str:
        self._client.upload_fileobj(
            body,
            self._settings.s3_bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return self._uri(key)

    def get_object(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._settings.s3_bucket, Key=key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def stream_object(self, key: str, dest: BinaryIO, chunk_size: int = 1024 * 1024) -> None:
        response = self._client.get_object(Bucket=self._settings.s3_bucket, Key=key)
        body = response["Body"]
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                dest.write(chunk)
        finally:
            body.close()

    def iter_object(self, key: str, chunk_size: int = 1024 * 1024):
        response = self._client.get_object(Bucket=self._settings.s3_bucket, Key=key)
        body = response["Body"]
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._settings.s3_bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._settings.s3_bucket, Key=key)
            return True
        except self._client.exceptions.ClientError:
            return False


def blob_storage_key(content_hash: str) -> str:
    return f"blobs/{content_hash[:2]}/{content_hash}"


# Sentinel storage_uri written to a blob row when GC has purged its physical
# storage but the row is retained because a reference reappeared mid-purge.
# The next upload hitting this hash re-creates storage and restores a real URI.
GC_PURGED_URI = "gc://purged"


def custom_parse_storage_key(job_id: str) -> str:
    """Object key for a custom (prompt-scoped) parse job result."""
    return f"custom-parse-results/{job_id}.json"


def parse_result_storage_key(content_hash: str, parser_name: str, profile: str) -> str:
    return f"parse-results/{content_hash[:2]}/{content_hash}/{parser_name}/{profile}.json"


def derivative_storage_key(
    content_hash: str,
    *,
    kind: str,
    version: str,
    filename: str,
) -> str:
    """派生产物对象键（非用户引用）。

    例：``derivatives/ab/abcd…/legacy-office/0.1.0/normalized.docx``

    - 绑定**原始** content_hash，随原 blob 生命周期 GC
    - 不是 FileReference，智能体默认不可见
    """
    safe_kind = kind.strip().replace("..", "").replace("/", "-")
    safe_version = version.strip().replace("..", "").replace("/", "-")
    safe_name = Path(filename).name
    return f"derivatives/{content_hash[:2]}/{content_hash}/{safe_kind}/{safe_version}/{safe_name}"


def storage_key_from_uri(uri: str) -> str:
    if uri.startswith("memory://"):
        return uri.removeprefix("memory://")
    if uri.startswith("local://"):
        return uri.removeprefix("local://")
    if uri.startswith("file://"):
        # Legacy absolute file URIs are not key-based; return path as-is for rare cases.
        return uri.removeprefix("file://")
    if uri.startswith("s3://"):
        parts = uri.split("/", 3)
        return parts[3] if len(parts) > 3 else uri
    return uri


class InMemoryStorageBackend(StorageBackend):
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_object(self, key: str, body: BinaryIO, content_type: str) -> str:
        self._objects[key] = body.read()
        return f"memory://{key}"

    def get_object(self, key: str) -> bytes:
        return self._objects[key]

    def stream_object(self, key: str, dest: BinaryIO, chunk_size: int = 1024 * 1024) -> None:
        data = self._objects.get(key, b"")
        dest.write(data)

    def iter_object(self, key: str, chunk_size: int = 1024 * 1024):
        data = self._objects.get(key, b"")
        offset = 0
        while offset < len(data):
            yield data[offset : offset + chunk_size]
            offset += chunk_size

    def delete_object(self, key: str) -> None:
        self._objects.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self._objects


_storage: StorageBackend | None = None


def create_storage(settings: Settings | None = None) -> StorageBackend:
    settings = settings or get_settings()
    backend = settings.storage_backend
    if backend == "local":
        return LocalFilesystemStorageBackend(settings)
    if backend == "s3":
        return S3StorageBackend(settings)
    if backend == "memory":
        return InMemoryStorageBackend()
    raise ValueError(f"unsupported storage_backend: {backend!r}")


def get_storage() -> StorageBackend:
    global _storage
    if _storage is None:
        _storage = create_storage()
    return _storage


def set_storage(backend: StorageBackend | None) -> None:
    global _storage
    _storage = backend
