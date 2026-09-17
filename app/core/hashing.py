import hashlib
import os
import tempfile
from typing import BinaryIO


# 1 MiB in-memory buffer before SpooledTemporaryFile spills to disk.
_SPOOL_MAX_BYTES = 1024 * 1024


class UploadTooLargeError(Exception):
    """Raised when streamed upload exceeds the configured hard limit."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"Upload size {size_bytes} exceeds max_upload_bytes={max_bytes}"
        )


class HashedSpooledFile:
    """A readable, rewindable view over a spooled temp file holding hashed bytes.

    The upload pipeline streams the request body into a spooled temp file while
    computing SHA-256. Small files stay in memory (fast path identical to the old
    io.BytesIO behaviour); large files spill to disk so peak RSS is bounded by
    ``_SPOOL_MAX_BYTES`` rather than by file size.

    Callers can either:
      - ``read_bytes()`` to materialise the full content (use only when a legacy
        ``bytes`` API genuinely needs it), or
      - ``open()`` / pass the object itself as a file-like to storage upload,
        avoiding any full materialisation.
    """

    def __init__(self, spool: tempfile.SpooledTemporaryFile[bytes], content_hash: str, size: int):
        self._spool = spool
        self.content_hash = content_hash
        self.size = size

    def seek(self, pos: int, whence: int = 0) -> int:
        return self._spool.seek(pos, whence)

    def read(self, n: int = -1) -> bytes:
        return self._spool.read(n)

    def write(self, b: bytes) -> int:
        return self._spool.write(b)

    def tell(self) -> int:
        return self._spool.tell()

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._spool.fileno()

    def read_bytes(self) -> bytes:
        """Return the full content as bytes. Avoid for large files."""
        self._spool.seek(0)
        return self._spool.read()

    def as_file_like(self) -> BinaryIO:
        """Return a seekable file-like positioned at 0 suitable for streaming upload."""
        self._spool.seek(0)
        return self._spool  # type: ignore[return-value]

    def read_range(self, n: int) -> bytes:
        """Read at most ``n`` bytes from the current position."""
        return self._spool.read(n)

    def close(self) -> None:
        self._spool.close()


def stream_sha256(file_obj: BinaryIO, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA-256 hex digest from a readable binary stream."""
    digest = hashlib.sha256()
    while True:
        chunk = file_obj.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def read_and_hash(
    file_obj: BinaryIO,
    chunk_size: int = 1024 * 1024,
    spool_max_bytes: int = _SPOOL_MAX_BYTES,
    max_bytes: int = 0,
) -> tuple[HashedSpooledFile, str, int]:
    """Stream ``file_obj`` into a spooled temp file while computing SHA-256.

    Returns ``(spooled, content_hash, size_bytes)``. The spooled file is rewound
    to position 0 and ready to be re-read (e.g. for storage upload). Caller is
    responsible for closing the spooled file via ``close()`` once done.

    If ``max_bytes > 0`` and the stream grows beyond that limit, aborts early and
    raises :class:`UploadTooLargeError` (temp spool is closed).
    """
    digest = hashlib.sha256()
    size = 0
    spool = tempfile.SpooledTemporaryFile(max_size=spool_max_bytes, mode="w+b", suffix="-upload")
    try:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if max_bytes > 0 and size > max_bytes:
                raise UploadTooLargeError(size_bytes=size, max_bytes=max_bytes)
            spool.write(chunk)
        spool.seek(0)
    except Exception:
        spool.close()
        raise
    return HashedSpooledFile(spool, digest.hexdigest(), size), digest.hexdigest(), size


def hash_file_path(file_path: str, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA-256 of a file on disk."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_head_bytes(file_obj: BinaryIO, n: int) -> bytes:
    """Read up to ``n`` bytes from the current position without consuming beyond."""
    pos = file_obj.tell()
    try:
        return file_obj.read(n)
    finally:
        file_obj.seek(pos)


def head_bytes_from_file(path: str | os.PathLike[str], n: int) -> bytes:
    """Read the first ``n`` bytes of a file on disk."""
    with open(path, "rb") as fh:
        return fh.read(n)