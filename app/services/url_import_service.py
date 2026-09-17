"""Fetch remote file by URL with SSRF protections, then hand off to upload path."""
from __future__ import annotations

import ipaddress
import socket
import tempfile
from pathlib import PurePosixPath
from urllib.parse import urlparse

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.storage import StorageBackend
from app.schemas.files import FromUrlRequest, UploadResponse
from app.services.file_service import upload_file


class UrlImportError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url_for_import(url: str, settings: Settings | None = None) -> str:
    """Validate URL scheme/host; resolve DNS and reject private targets."""
    settings = settings or get_settings()
    raw = (url or "").strip()
    if not raw:
        raise UrlImportError("from_url_invalid", "URL is required")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise UrlImportError("from_url_invalid_scheme", "Only http/https URLs are allowed")
    if not parsed.hostname:
        raise UrlImportError("from_url_invalid", "URL host is required")
    host = parsed.hostname.lower()
    allowlist = settings.from_url_host_allowlist
    if allowlist is not None and host not in allowlist:
        raise UrlImportError("from_url_host_not_allowed", f"Host not in allowlist: {host}")

    # Literal IP in hostname
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise UrlImportError("from_url_ssrf_blocked", "URL target is not allowed")
        return raw
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UrlImportError("from_url_dns_failed", f"DNS resolution failed: {host}") from exc
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise UrlImportError("from_url_ssrf_blocked", "URL resolves to a blocked address")
    return raw


def _filename_from_url(url: str, explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    path = urlparse(url).path or ""
    name = PurePosixPath(path).name
    return name or "download.bin"


def _consume_response(resp, url: str, spool, max_upload: int) -> tuple[tempfile.SpooledTemporaryFile, str | None]:
    if resp.status_code >= 400:
        raise UrlImportError(
            "from_url_http_error",
            f"Remote returned HTTP {resp.status_code}",
            http_status=502,
        )
    content_type = resp.headers.get("content-type")
    if content_type and ";" in content_type:
        content_type = content_type.split(";", 1)[0].strip()
    total = 0
    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if max_upload > 0 and total > max_upload:
            raise UrlImportError(
                "from_url_too_large",
                f"Remote file exceeds max_upload_bytes={max_upload}",
                http_status=413,
            )
        spool.write(chunk)
    spool.seek(0)
    return spool, content_type


def download_url_to_spool(
    url: str,
    *,
    settings: Settings | None = None,
    max_bytes: int = 0,
) -> tuple[tempfile.SpooledTemporaryFile, str, str | None]:
    """Stream URL body into a spooled temp file. Returns (file, filename, content_type)."""
    import httpx

    settings = settings or get_settings()
    safe_url = validate_url_for_import(url, settings)
    timeout = settings.from_url_timeout
    max_upload = max_bytes if max_bytes > 0 else settings.max_upload_bytes

    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream("GET", safe_url) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not location:
                        raise UrlImportError("from_url_http_error", "Redirect without Location")
                    # Relative redirects
                    if location.startswith("/"):
                        parsed = urlparse(safe_url)
                        location = f"{parsed.scheme}://{parsed.netloc}{location}"
                    safe_url = validate_url_for_import(location, settings)
                    with client.stream("GET", safe_url) as resp2:
                        spool, content_type = _consume_response(resp2, safe_url, spool, max_upload)
                        return spool, _filename_from_url(safe_url, None), content_type
                spool, content_type = _consume_response(resp, safe_url, spool, max_upload)
                return spool, _filename_from_url(safe_url, None), content_type
    except UrlImportError:
        spool.close()
        raise
    except httpx.HTTPError as exc:
        spool.close()
        raise UrlImportError("from_url_http_error", f"HTTP fetch failed: {exc}", http_status=502) from exc


def import_from_url(
    db: Session,
    storage: StorageBackend,
    *,
    tenant_id: str,
    body: FromUrlRequest,
    background_tasks: BackgroundTasks | None = None,
    settings: Settings | None = None,
) -> UploadResponse:
    settings = settings or get_settings()
    if not settings.from_url_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="from-url is disabled")

    try:
        spool, guessed_name, content_type = download_url_to_spool(
            body.url,
            settings=settings,
            max_bytes=settings.max_upload_bytes,
        )
    except UrlImportError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"message": exc.message, "message_code": exc.code},
        ) from exc

    filename = body.filename or guessed_name
    try:
        return upload_file(
            db,
            storage,
            tenant_id=tenant_id,
            file_obj=spool,
            filename=filename,
            content_type=content_type,
            session_id=body.session_id,
            agent_id=body.agent_id,
            uploaded_by=body.uploaded_by,
            custom_metadata=dict(body.metadata or {}),
            auto_parse=body.auto_parse,
            expires_at=body.expires_at,
            background_tasks=background_tasks,
        )
    finally:
        spool.close()
