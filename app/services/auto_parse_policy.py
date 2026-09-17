"""按文件类型决定是否自动解析，以及使用哪个 profile。

上传参数 ``auto_parse=true`` 仍受本策略约束；显式 ``POST /parse`` 不受限。
图片默认 profile=deep（视觉识别）；文档类默认 layout→Markdown。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.parsers.types import FileKind


@dataclass(frozen=True)
class AutoParseDecision:
    """自动解析门闸结果。"""

    should_parse: bool
    kind: str
    reason: str
    profile: str = "layout"
    max_pages: int | None = None
    max_bytes: int | None = None
    max_sheets: int | None = None
    page_count: int | None = None
    size_bytes: int | None = None
    sheet_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_parse": self.should_parse,
            "kind": self.kind,
            "reason": self.reason,
            "profile": self.profile,
            "max_pages": self.max_pages,
            "max_bytes": self.max_bytes,
            "max_sheets": self.max_sheets,
            "page_count": self.page_count,
            "size_bytes": self.size_bytes,
            "sheet_count": self.sheet_count,
        }


# reason codes（稳定，供客户端/Agent 分支）
REASON_USER_DISABLED = "user_disabled"
REASON_ENABLED = "enabled"
REASON_DISABLED_BY_CONFIG = "disabled_by_config"
REASON_EXCEEDS_MAX_PAGES = "exceeds_max_pages"
REASON_EXCEEDS_MAX_BYTES = "exceeds_max_bytes"
REASON_EXCEEDS_MAX_SHEETS = "exceeds_max_sheets"

_REASON_MESSAGES: dict[str, str] = {
    REASON_USER_DISABLED: "上传时关闭了自动解析（auto_parse=false）。",
    REASON_ENABLED: "已按策略自动触发解析。",
    REASON_DISABLED_BY_CONFIG: "该文件类型未开启自动解析。",
    REASON_EXCEEDS_MAX_PAGES: "页数超过自动解析门槛，已跳过；可手动触发解析。",
    REASON_EXCEEDS_MAX_BYTES: "文件大小超过自动解析门槛，已跳过；可手动触发解析。",
    REASON_EXCEEDS_MAX_SHEETS: "工作表数量超过自动解析门槛，已跳过；可手动触发解析。",
}


def reason_message(reason: str) -> str:
    return _REASON_MESSAGES.get(reason, reason)


def _kind_limits(kind: str, settings: Settings) -> tuple[bool, str, int, int, int]:
    """Return (enabled, profile, max_pages, max_bytes, max_sheets). 0 means unlimited."""
    k = (kind or FileKind.UNKNOWN.value).lower()
    if k == FileKind.PDF.value:
        return (
            settings.auto_parse_pdf_enabled,
            settings.auto_parse_pdf_profile,
            settings.auto_parse_pdf_max_pages,
            settings.auto_parse_pdf_max_bytes,
            0,
        )
    if k == FileKind.SPREADSHEET.value:
        return (
            settings.auto_parse_spreadsheet_enabled,
            settings.auto_parse_spreadsheet_profile,
            settings.auto_parse_spreadsheet_max_pages,
            settings.auto_parse_spreadsheet_max_bytes,
            settings.auto_parse_spreadsheet_max_sheets,
        )
    if k == FileKind.OFFICE.value:
        return (
            settings.auto_parse_office_enabled,
            settings.auto_parse_office_profile,
            settings.auto_parse_office_max_pages,
            settings.auto_parse_office_max_bytes,
            0,
        )
    if k == FileKind.TEXT.value:
        return (
            settings.auto_parse_text_enabled,
            settings.auto_parse_text_profile,
            settings.auto_parse_text_max_pages,
            settings.auto_parse_text_max_bytes,
            0,
        )
    if k == FileKind.IMAGE.value:
        return (
            settings.auto_parse_image_enabled,
            settings.auto_parse_image_profile,
            settings.auto_parse_image_max_pages,
            settings.auto_parse_image_max_bytes,
            0,
        )
    if k == FileKind.AUDIO.value:
        return (
            settings.auto_parse_audio_enabled,
            settings.auto_parse_audio_profile,
            settings.auto_parse_audio_max_pages,
            settings.auto_parse_audio_max_bytes,
            0,
        )
    if k == FileKind.VIDEO.value:
        return (
            settings.auto_parse_video_enabled,
            settings.auto_parse_video_profile,
            settings.auto_parse_video_max_pages,
            settings.auto_parse_video_max_bytes,
            0,
        )
    if k == FileKind.ARCHIVE.value:
        # 父压缩包本身不跑 layout；子文件各自按类型策略
        return (False, "layout", 0, 0, 0)
    # unknown / fallback
    return (
        settings.auto_parse_unknown_enabled,
        settings.auto_parse_unknown_profile,
        settings.auto_parse_unknown_max_pages,
        settings.auto_parse_unknown_max_bytes,
        0,
    )


def decide_auto_parse(
    *,
    auto_parse_requested: bool,
    file_kind: str,
    size_bytes: int,
    page_count: int | None = None,
    sheet_count: int | None = None,
    settings: Settings | None = None,
) -> AutoParseDecision:
    """Decide whether to schedule parse after upload, and which profile."""
    settings = settings or get_settings()
    kind = (file_kind or FileKind.UNKNOWN.value).lower()
    enabled, profile, max_pages, max_bytes, max_sheets = _kind_limits(kind, settings)

    def _base(**overrides: Any) -> AutoParseDecision:
        data = dict(
            should_parse=False,
            kind=kind,
            reason=REASON_USER_DISABLED,
            profile=profile,
            max_pages=max_pages or None,
            max_bytes=max_bytes or None,
            max_sheets=max_sheets or None,
            page_count=page_count,
            size_bytes=size_bytes,
            sheet_count=sheet_count,
        )
        data.update(overrides)
        return AutoParseDecision(**data)

    if not auto_parse_requested:
        return _base(reason=REASON_USER_DISABLED)

    if not enabled:
        return _base(reason=REASON_DISABLED_BY_CONFIG)

    if max_bytes > 0 and size_bytes > max_bytes:
        return _base(reason=REASON_EXCEEDS_MAX_BYTES, max_bytes=max_bytes)

    if max_pages > 0 and page_count is not None and page_count > max_pages:
        return _base(reason=REASON_EXCEEDS_MAX_PAGES, max_pages=max_pages)

    if max_sheets > 0 and sheet_count is not None and sheet_count > max_sheets:
        return _base(reason=REASON_EXCEEDS_MAX_SHEETS, max_sheets=max_sheets)

    return _base(should_parse=True, reason=REASON_ENABLED)
