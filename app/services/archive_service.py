"""压缩包上传即解压（简单版）。

产品约定：
- 始终保留父包 blob + reference
- 过滤隐藏 / 不支持类型后，可识别成员 ≤ max 则全部展开为子 reference
- 过多则只存父包（too_many），不拒绝上传
- zip 可选密码（ZipCrypto）；密码不落库、不写日志
"""
from __future__ import annotations

import io
import mimetypes
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import BinaryIO

from app.config import Settings, get_settings

# 状态码（稳定，供客户端 / Agent 分支）
STATUS_EXPANDED = "expanded"
STATUS_TOO_MANY = "too_many"
STATUS_EMPTY = "empty"
STATUS_PASSWORD_REQUIRED = "password_required"
STATUS_PASSWORD_INCORRECT = "password_incorrect"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"

# 跳过原因
SKIP_HIDDEN = "hidden"
SKIP_UNSUPPORTED = "unsupported_type"
SKIP_DIR = "directory"
SKIP_UNSAFE_PATH = "unsafe_path"
SKIP_TOO_LARGE = "member_too_large"
SKIP_NESTED_ARCHIVE = "nested_archive"
SKIP_EMPTY = "empty_file"


class ArchiveError(Exception):
    """用户可理解的压缩包错误（映射 HTTP 400）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArchiveMember:
    path: str  # 规范化相对路径，POSIX
    size: int
    is_dir: bool = False


@dataclass
class ArchiveInspectResult:
    status: str
    format: str
    eligible: list[ArchiveMember] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    eligible_count: int = 0
    skipped_count: int = 0
    max_expand_files: int = 8
    message: str = ""
    encrypted: bool = False


def detect_archive_format(filename: str, mime_type: str = "") -> str | None:
    name = (filename or "").lower()
    mime = (mime_type or "").lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar.gz"
    if name.endswith(".tar.bz2") or name.endswith(".tbz2"):
        return "tar.bz2"
    if name.endswith(".tar") or mime in {"application/x-tar", "application/tar"}:
        return "tar"
    if name.endswith(".zip") or mime in {
        "application/zip",
        "application/x-zip-compressed",
        "multipart/x-zip",
    }:
        return "zip"
    return None


def is_archive_filename(filename: str, mime_type: str = "") -> bool:
    return detect_archive_format(filename, mime_type) is not None


def _default_supported_extensions(settings: Settings) -> set[str]:
    raw = (settings.archive_supported_extensions or "").strip()
    if not raw:
        return {
            ".pdf",
            ".docx",
            ".doc",
            ".pptx",
            ".ppt",
            ".xlsx",
            ".xls",
            ".et",
            ".ett",
            ".wps",
            ".wpt",
            ".dps",
            ".dpt",
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".bmp",
            ".wav",
            ".mp3",
            ".m4a",
            ".ogg",
            ".flac",
            ".mp4",
            ".webm",
            ".mov",
        }
    out: set[str] = set()
    for part in raw.split(","):
        p = part.strip().lower()
        if not p:
            continue
        if not p.startswith("."):
            p = f".{p}"
        out.add(p)
    return out


def _normalize_member_path(raw: str) -> str | None:
    """Return safe relative POSIX path or None if unsafe."""
    if raw is None:
        return None
    # zip 可能用反斜杠
    text = str(raw).replace("\\", "/").strip()
    if not text or text.endswith("/"):
        # 目录交给 is_dir 路径处理；纯目录名返回去尾 /
        text = text.rstrip("/")
        if not text:
            return None
    path = PurePosixPath(text)
    if path.is_absolute() or str(path).startswith("/"):
        return None
    parts = path.parts
    if any(p in {"", ".", ".."} for p in parts):
        return None
    if any(p.startswith("..") for p in parts):
        return None
    return str(PurePosixPath(*parts))


def _is_hidden_path(path: str) -> bool:
    name = PurePosixPath(path).name
    if name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
        return True
    for part in PurePosixPath(path).parts:
        if part in {"__MACOSX", ".git", ".svn", ".hg"}:
            return True
        if part.startswith(".") and part not in {".", ".."}:
            return True
    return False


def _is_nested_archive(path: str) -> bool:
    return detect_archive_format(path) is not None


def _is_supported_file(path: str, supported: set[str]) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    # .tar.gz 等双后缀：detect_archive 已拦；普通文件看最后后缀
    if path.lower().endswith(".tar.gz") or path.lower().endswith(".tar.bz2"):
        return False
    return suffix in supported


def guess_member_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _classify_member(
    path: str,
    size: int,
    *,
    is_dir: bool,
    supported: set[str],
    max_member_bytes: int,
) -> tuple[str | None, str | None]:
    """Return (skip_reason, None) or (None, ok)."""
    if is_dir:
        return SKIP_DIR, None
    if _is_hidden_path(path):
        return SKIP_HIDDEN, None
    if _is_nested_archive(path):
        return SKIP_NESTED_ARCHIVE, None
    if not _is_supported_file(path, supported):
        return SKIP_UNSUPPORTED, None
    if size <= 0:
        return SKIP_EMPTY, None
    if max_member_bytes > 0 and size > max_member_bytes:
        return SKIP_TOO_LARGE, None
    return None, "ok"


def _zip_is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


def _inspect_zip(
    file_obj: BinaryIO,
    *,
    password: str | None,
    settings: Settings,
) -> ArchiveInspectResult:
    supported = _default_supported_extensions(settings)
    max_files = settings.archive_max_expand_files
    max_member = settings.archive_max_member_bytes
    max_total = settings.archive_max_total_bytes

    try:
        zf = zipfile.ZipFile(file_obj)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(STATUS_FAILED, "不是有效的 zip 文件。") from exc

    pwd = password.encode("utf-8") if password else None
    encrypted_any = False
    eligible: list[ArchiveMember] = []
    skipped: list[dict[str, str]] = []
    total_eligible_bytes = 0

    with zf:
        for info in zf.infolist():
            if _zip_is_encrypted(info):
                encrypted_any = True
            raw_name = info.filename
            is_dir = raw_name.endswith("/") or info.is_dir()
            path = _normalize_member_path(raw_name)
            if path is None:
                skipped.append({"path": raw_name, "reason": SKIP_UNSAFE_PATH})
                continue
            size = int(info.file_size or 0)
            skip, _ = _classify_member(
                path, size, is_dir=is_dir, supported=supported, max_member_bytes=max_member
            )
            if skip:
                skipped.append({"path": path, "reason": skip})
                continue

            # 加密成员：需要密码才能读
            if _zip_is_encrypted(info) and not pwd:
                raise ArchiveError(
                    STATUS_PASSWORD_REQUIRED,
                    "该 zip 含加密文件，请在上传时提供 archive_password。",
                )

            # 试读头部校验密码（避免错误密码在 expand 阶段才爆）
            if pwd and _zip_is_encrypted(info):
                try:
                    with zf.open(info, pwd=pwd) as fh:
                        fh.read(1)
                except RuntimeError as exc:
                    # zipfile: "Bad password for file"
                    msg = str(exc).lower()
                    if "password" in msg or "bad" in msg:
                        raise ArchiveError(
                            STATUS_PASSWORD_INCORRECT,
                            "zip 密码不正确。",
                        ) from exc
                    raise ArchiveError(STATUS_FAILED, "无法读取 zip 内加密文件。") from exc
                except zipfile.BadZipFile as exc:
                    raise ArchiveError(STATUS_FAILED, "zip 文件损坏或无法读取。") from exc

            if max_total > 0 and total_eligible_bytes + size > max_total:
                skipped.append({"path": path, "reason": SKIP_TOO_LARGE})
                continue

            eligible.append(ArchiveMember(path=path, size=size, is_dir=False))
            total_eligible_bytes += size

    return _decide(
        format="zip",
        eligible=eligible,
        skipped=skipped,
        max_files=max_files,
        encrypted=encrypted_any,
    )


def _inspect_tar(
    file_obj: BinaryIO,
    *,
    format_name: str,
    settings: Settings,
) -> ArchiveInspectResult:
    supported = _default_supported_extensions(settings)
    max_files = settings.archive_max_expand_files
    max_member = settings.archive_max_member_bytes
    max_total = settings.archive_max_total_bytes

    mode = "r:*"
    try:
        tf = tarfile.open(fileobj=file_obj, mode=mode)
    except tarfile.TarError as exc:
        raise ArchiveError(STATUS_FAILED, f"不是有效的 {format_name} 文件。") from exc

    eligible: list[ArchiveMember] = []
    skipped: list[dict[str, str]] = []
    total_eligible_bytes = 0

    with tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                skipped.append({"path": member.name, "reason": SKIP_UNSAFE_PATH})
                continue
            is_dir = member.isdir()
            path = _normalize_member_path(member.name)
            if path is None:
                skipped.append({"path": member.name, "reason": SKIP_UNSAFE_PATH})
                continue
            size = int(member.size or 0)
            skip, _ = _classify_member(
                path, size, is_dir=is_dir, supported=supported, max_member_bytes=max_member
            )
            if skip:
                skipped.append({"path": path, "reason": skip})
                continue
            if max_total > 0 and total_eligible_bytes + size > max_total:
                skipped.append({"path": path, "reason": SKIP_TOO_LARGE})
                continue
            eligible.append(ArchiveMember(path=path, size=size, is_dir=False))
            total_eligible_bytes += size

    return _decide(
        format=format_name,
        eligible=eligible,
        skipped=skipped,
        max_files=max_files,
        encrypted=False,
    )


def _decide(
    *,
    format: str,
    eligible: list[ArchiveMember],
    skipped: list[dict[str, str]],
    max_files: int,
    encrypted: bool,
) -> ArchiveInspectResult:
    count = len(eligible)
    if count == 0:
        return ArchiveInspectResult(
            status=STATUS_EMPTY,
            format=format,
            eligible=[],
            skipped=skipped,
            eligible_count=0,
            skipped_count=len(skipped),
            max_expand_files=max_files,
            message="压缩包内没有可识别的文档（已忽略隐藏文件与不支持类型）。",
            encrypted=encrypted,
        )
    if count > max_files:
        return ArchiveInspectResult(
            status=STATUS_TOO_MANY,
            format=format,
            eligible=[],  # 过多时不解压
            skipped=skipped,
            eligible_count=count,
            skipped_count=len(skipped),
            max_expand_files=max_files,
            message=(
                f"可识别文件共 {count} 个，超过上限 {max_files}，"
                "已仅保存压缩包本身，请拆包后分批上传。"
            ),
            encrypted=encrypted,
        )
    return ArchiveInspectResult(
        status=STATUS_EXPANDED,
        format=format,
        eligible=eligible,
        skipped=skipped,
        eligible_count=count,
        skipped_count=len(skipped),
        max_expand_files=max_files,
        message=f"已解压 {count} 个可识别文件。",
        encrypted=encrypted,
    )


def inspect_archive(
    file_obj: BinaryIO,
    *,
    filename: str,
    mime_type: str = "",
    password: str | None = None,
    settings: Settings | None = None,
) -> ArchiveInspectResult:
    """List + filter members; decide expand / too_many / empty. Does not write storage."""
    settings = settings or get_settings()
    if not settings.archive_expand_enabled:
        fmt = detect_archive_format(filename, mime_type) or "unknown"
        return ArchiveInspectResult(
            status=STATUS_DISABLED,
            format=fmt,
            message="压缩包自动解压已关闭（archive_expand_enabled=false）。",
        )

    fmt = detect_archive_format(filename, mime_type)
    if fmt is None:
        raise ArchiveError(STATUS_FAILED, "无法识别压缩包格式。")

    if hasattr(file_obj, "seek"):
        try:
            file_obj.seek(0)
        except (io.UnsupportedOperation, OSError):
            pass

    if fmt == "zip":
        return _inspect_zip(file_obj, password=password, settings=settings)
    return _inspect_tar(file_obj, format_name=fmt, settings=settings)


def extract_member_bytes(
    file_obj: BinaryIO,
    *,
    filename: str,
    member_path: str,
    password: str | None = None,
    mime_type: str = "",
    max_member_bytes: int = 0,
) -> bytes:
    """Read one member's bytes from archive (password only for zip)."""
    fmt = detect_archive_format(filename, mime_type)
    if fmt is None:
        raise ArchiveError(STATUS_FAILED, "无法识别压缩包格式。")

    if hasattr(file_obj, "seek"):
        try:
            file_obj.seek(0)
        except (io.UnsupportedOperation, OSError):
            pass

    if fmt == "zip":
        pwd = password.encode("utf-8") if password else None
        with zipfile.ZipFile(file_obj) as zf:
            # zip 内路径可能是反斜杠
            target = None
            for info in zf.infolist():
                norm = _normalize_member_path(info.filename)
                if norm == member_path:
                    target = info
                    break
            if target is None:
                raise ArchiveError(STATUS_FAILED, f"压缩包内找不到成员：{member_path}")
            try:
                data = zf.read(target, pwd=pwd)
            except RuntimeError as exc:
                msg = str(exc).lower()
                if "password" in msg:
                    raise ArchiveError(STATUS_PASSWORD_INCORRECT, "zip 密码不正确。") from exc
                raise ArchiveError(STATUS_FAILED, "读取 zip 成员失败。") from exc
            if max_member_bytes > 0 and len(data) > max_member_bytes:
                raise ArchiveError(STATUS_FAILED, f"成员超过大小上限：{member_path}")
            return data

    with tarfile.open(fileobj=file_obj, mode="r:*") as tf:
        member = None
        for m in tf.getmembers():
            norm = _normalize_member_path(m.name)
            if norm == member_path and m.isfile():
                member = m
                break
        if member is None:
            raise ArchiveError(STATUS_FAILED, f"压缩包内找不到成员：{member_path}")
        extracted = tf.extractfile(member)
        if extracted is None:
            raise ArchiveError(STATUS_FAILED, f"无法读取成员：{member_path}")
        data = extracted.read()
        if max_member_bytes > 0 and len(data) > max_member_bytes:
            raise ArchiveError(STATUS_FAILED, f"成员超过大小上限：{member_path}")
        return data


def extract_members_batch(
    file_obj: BinaryIO,
    *,
    filename: str,
    member_paths: list[str],
    password: str | None = None,
    mime_type: str = "",
    max_member_bytes: int = 0,
) -> dict[str, bytes]:
    """Extract multiple members in one archive open. Keys are member paths."""
    if not member_paths:
        return {}
    wanted = set(member_paths)
    fmt = detect_archive_format(filename, mime_type)
    if fmt is None:
        raise ArchiveError(STATUS_FAILED, "无法识别压缩包格式。")

    if hasattr(file_obj, "seek"):
        try:
            file_obj.seek(0)
        except (io.UnsupportedOperation, OSError):
            pass

    out: dict[str, bytes] = {}
    if fmt == "zip":
        pwd = password.encode("utf-8") if password else None
        with zipfile.ZipFile(file_obj) as zf:
            by_norm: dict[str, zipfile.ZipInfo] = {}
            for info in zf.infolist():
                norm = _normalize_member_path(info.filename)
                if norm and norm in wanted:
                    by_norm[norm] = info
            for path in member_paths:
                target = by_norm.get(path)
                if target is None:
                    raise ArchiveError(STATUS_FAILED, f"压缩包内找不到成员：{path}")
                try:
                    data = zf.read(target, pwd=pwd)
                except RuntimeError as exc:
                    msg = str(exc).lower()
                    if "password" in msg:
                        raise ArchiveError(STATUS_PASSWORD_INCORRECT, "zip 密码不正确。") from exc
                    raise ArchiveError(STATUS_FAILED, "读取 zip 成员失败。") from exc
                if max_member_bytes > 0 and len(data) > max_member_bytes:
                    raise ArchiveError(STATUS_FAILED, f"成员超过大小上限：{path}")
                out[path] = data
        return out

    with tarfile.open(fileobj=file_obj, mode="r:*") as tf:
        by_norm: dict[str, tarfile.TarInfo] = {}
        for m in tf.getmembers():
            norm = _normalize_member_path(m.name)
            if norm and norm in wanted and m.isfile():
                by_norm[norm] = m
        for path in member_paths:
            member = by_norm.get(path)
            if member is None:
                raise ArchiveError(STATUS_FAILED, f"压缩包内找不到成员：{path}")
            extracted = tf.extractfile(member)
            if extracted is None:
                raise ArchiveError(STATUS_FAILED, f"无法读取成员：{path}")
            data = extracted.read()
            if max_member_bytes > 0 and len(data) > max_member_bytes:
                raise ArchiveError(STATUS_FAILED, f"成员超过大小上限：{path}")
            out[path] = data
    return out


def archive_summary_dict(result: ArchiveInspectResult) -> dict:
    """Compact summary for metadata.extra / API (no password)."""
    return {
        "status": result.status,
        "format": result.format,
        "eligible_count": result.eligible_count,
        "skipped_count": result.skipped_count,
        "max_expand_files": result.max_expand_files,
        "message": result.message,
        "encrypted": result.encrypted,
        "skipped_sample": result.skipped[:20],
    }
