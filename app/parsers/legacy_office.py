"""旧版 Office（doc/ppt/xls）与金山 WPS（wps/et/dps）→ OOXML 规范化。

国企场景大量二进制旧格式与 WPS 原生格式；MarkItDown 只能稳定处理 docx/pptx/xlsx。
与 file_convert 对齐：LibreOffice headless 先转再解析（WPS 为尽力而为）。

存储约定（见 docs/legacy-office.md）：
- 用户可见身份永远是**原始上传** blob + FileReference
- 转换后的 OOXML 是**派生产物**（derivatives/），可选缓存，不建第二引用
- 原 blob GC 时一并清理 derivatives
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.config import Settings, get_settings
from app.core.storage import StorageBackend, derivative_storage_key

logger = logging.getLogger(__name__)

# 旧扩展名 / WPS → LibreOffice --convert-to 目标（WPS 为尽力而为）
LEGACY_TO_OOXML: dict[str, str] = {
    "doc": "docx",
    "ppt": "pptx",
    "xls": "xlsx",
    # 金山 WPS 原生格式 → OOXML
    "wps": "docx",
    "wpt": "docx",
    "et": "xlsx",
    "ett": "xlsx",
    "dps": "pptx",
    "dpt": "pptx",
}

LEGACY_MIME_HINTS: dict[str, str] = {
    "application/msword": "doc",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.ms-excel": "xls",
    # 金山 WPS（勿映射 application/vnd.ms-works——那是微软 Works）
    "application/wps-office.wps": "wps",
    "application/wps-office.wpt": "wpt",
    "application/kswps": "wps",
    "application/wps-office.et": "et",
    "application/wps-office.ett": "ett",
    "application/kbdspread": "et",
    "application/wps-office.dps": "dps",
    "application/wps-office.dpt": "dpt",
    "application/kdpresentation": "dps",
}

CONVERTER_NAME = "libreoffice"
CONVERTER_VERSION = "0.1.0"


@dataclass(frozen=True)
class LegacyNormalizeResult:
    """规范化结果：供 layout 使用的本地路径 + 元数据。"""

    path: str
    source_format: str
    target_format: str
    cache_hit: bool
    converter: str = CONVERTER_NAME
    converter_version: str = CONVERTER_VERSION
    # 解析结束后应 unlink 的临时文件（不含原始 download 路径）
    temp_paths: tuple[str, ...] = ()


def detect_legacy_office_format(filename: str, mime_type: str | None = None) -> str | None:
    """若为需转换的旧式/WPS Office，返回源格式键（如 ``doc`` / ``et`` / ``wps``），否则 None。"""
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if suffix in LEGACY_TO_OOXML:
        return suffix
    mime = (mime_type or "").split(";")[0].strip().lower()
    return LEGACY_MIME_HINTS.get(mime)


def needs_legacy_conversion(filename: str, mime_type: str | None = None) -> bool:
    return detect_legacy_office_format(filename, mime_type) is not None


def resolve_soffice_binary(settings: Settings | None = None) -> str | None:
    """返回可用的 soffice/libreoffice 可执行路径。"""
    settings = settings or get_settings()
    configured = (settings.legacy_office_binary or "").strip()
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    candidates.extend(["soffice", "libreoffice"])
    for name in candidates:
        path = shutil.which(name) if os.path.sep not in name else (name if os.path.isfile(name) else None)
        if path:
            return path
    # 绝对路径配置但 which 未找到
    if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
        return configured
    return None


def convert_with_libreoffice(
    input_path: str,
    target_format: str,
    *,
    output_dir: str,
    binary: str,
    timeout: int = 300,
) -> str:
    """调用 LibreOffice 转换，返回输出文件路径。"""
    if target_format not in {"docx", "pptx", "xlsx", "pdf"}:
        raise ValueError(f"unsupported LibreOffice target format: {target_format}")

    os.makedirs(output_dir, exist_ok=True)
    # 独立 UserInstallation，避免并发 soffice 抢 profile 锁
    profile_dir = tempfile.mkdtemp(prefix="fs-lo-profile-")
    profile_uri = Path(profile_dir).resolve().as_uri()
    cmd = [
        binary,
        "--headless",
        "--norestore",
        "--nolockcheck",
        f"-env:UserInstallation={profile_uri}",
        "--convert-to",
        target_format,
        "--outdir",
        output_dir,
        input_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "HOME": profile_dir},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"LibreOffice conversion timed out after {timeout}s "
            f"(target={target_format}, input={Path(input_path).name})"
        ) from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()[:800]
        raise RuntimeError(
            f"LibreOffice conversion failed (exit={result.returncode}, "
            f"target={target_format}): {stderr or 'no stderr'}"
        )

    stem = Path(input_path).stem
    output_path = Path(output_dir) / f"{stem}.{target_format}"
    if not output_path.is_file():
        # 偶发大小写/命名差异：扫描 outdir
        matches = list(Path(output_dir).glob(f"*.{target_format}"))
        if len(matches) == 1:
            output_path = matches[0]
        else:
            raise RuntimeError(
                f"LibreOffice output not found: expected {output_path.name} in {output_dir}"
            )
    return str(output_path)


def _derivative_object_name(target_format: str, converter_version: str) -> str:
    return f"normalized.{target_format}"


def ensure_normalized_office(
    source_path: str,
    *,
    filename: str,
    mime_type: str | None,
    content_hash: str,
    storage: StorageBackend,
    settings: Settings | None = None,
) -> LegacyNormalizeResult | None:
    """若需要，将旧 Office 转为 OOXML 并可选写入派生产物缓存。

    返回 None 表示无需转换（非 legacy）。
    """
    settings = settings or get_settings()
    if not settings.legacy_office_enabled:
        return None

    source_format = detect_legacy_office_format(filename, mime_type)
    if source_format is None:
        return None

    target_format = LEGACY_TO_OOXML[source_format]
    converter_version = settings.legacy_office_converter_version or CONVERTER_VERSION
    object_name = _derivative_object_name(target_format, converter_version)
    cache_key = derivative_storage_key(
        content_hash,
        kind="legacy-office",
        version=converter_version,
        filename=object_name,
    )

    temp_paths: list[str] = []

    # 1) 缓存命中：拉到临时文件再交给 MarkItDown
    if settings.legacy_office_cache_enabled and storage.exists(cache_key):
        tmp = tempfile.NamedTemporaryFile(
            suffix=f".{target_format}",
            delete=False,
            prefix="fs-legacy-cached-",
        )
        try:
            storage.stream_object(cache_key, tmp)
            tmp.flush()
        except Exception:
            tmp.close()
            Path(tmp.name).unlink(missing_ok=True)
            raise
        else:
            tmp.close()
            temp_paths.append(tmp.name)
            logger.info(
                "legacy office cache hit content_hash=%s %s→%s",
                content_hash[:12],
                source_format,
                target_format,
            )
            return LegacyNormalizeResult(
                path=tmp.name,
                source_format=source_format,
                target_format=target_format,
                cache_hit=True,
                converter_version=converter_version,
                temp_paths=tuple(temp_paths),
            )

    binary = resolve_soffice_binary(settings)
    if not binary:
        raise RuntimeError(
            "Legacy Office conversion required for "
            f".{source_format}, but LibreOffice (soffice) is not available. "
            "Install LibreOffice or set LEGACY_OFFICE_BINARY / legacy_office_binary."
        )

    work_dir = tempfile.mkdtemp(prefix="fs-legacy-work-")
    temp_paths.append(work_dir)
    try:
        # 保证输入带正确扩展名（LibreOffice 依赖扩展名识别）
        input_with_ext = Path(work_dir) / f"source.{source_format}"
        if Path(source_path).resolve() != input_with_ext.resolve():
            shutil.copy2(source_path, input_with_ext)
        converted = convert_with_libreoffice(
            str(input_with_ext),
            target_format,
            output_dir=work_dir,
            binary=binary,
            timeout=settings.legacy_office_timeout,
        )

        if settings.legacy_office_cache_enabled:
            with open(converted, "rb") as fh:
                storage.put_object(
                    cache_key,
                    fh,  # type: ignore[arg-type]
                    _mime_for_ooxml(target_format),
                )
            logger.info(
                "legacy office cached content_hash=%s key=%s",
                content_hash[:12],
                cache_key,
            )

        # 把转换结果拷到独立临时文件，以便可删整个 work_dir
        out_tmp = tempfile.NamedTemporaryFile(
            suffix=f".{target_format}",
            delete=False,
            prefix="fs-legacy-out-",
        )
        out_tmp.close()
        shutil.copy2(converted, out_tmp.name)
        temp_paths.append(out_tmp.name)

        return LegacyNormalizeResult(
            path=out_tmp.name,
            source_format=source_format,
            target_format=target_format,
            cache_hit=False,
            converter_version=converter_version,
            temp_paths=tuple(temp_paths),
        )
    except Exception:
        _cleanup_paths(temp_paths)
        raise


def cleanup_legacy_normalize(result: LegacyNormalizeResult | None) -> None:
    if result is None:
        return
    _cleanup_paths(result.temp_paths)


def _cleanup_paths(paths: list[str] | tuple[str, ...]) -> None:
    for p in paths:
        path = Path(p)
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _mime_for_ooxml(target_format: str) -> str:
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(target_format, "application/octet-stream")


def purge_legacy_derivatives(
    storage: StorageBackend,
    content_hash: str,
    *,
    converter_version: str | None = None,
    settings: Settings | None = None,
) -> None:
    """原 blob GC 时删除已知 legacy 派生产物键。"""
    settings = settings or get_settings()
    version = converter_version or settings.legacy_office_converter_version or CONVERTER_VERSION
    for target in LEGACY_TO_OOXML.values():
        key = derivative_storage_key(
            content_hash,
            kind="legacy-office",
            version=version,
            filename=_derivative_object_name(target, version),
        )
        try:
            storage.delete_object(key)
        except Exception:
            logger.debug("purge derivative failed key=%s", key, exc_info=True)


def open_binary_for_classify(file_data: BinaryIO | bytes) -> BinaryIO:
    """兼容 helpers（测试用）。"""
    import io

    if isinstance(file_data, (bytes, bytearray, memoryview)):
        return io.BytesIO(file_data)
    return file_data
