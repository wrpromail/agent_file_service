"""自动解析分类型开关、门槛与 profile。"""
from app.config import Settings
from app.services.auto_parse_policy import (
    REASON_DISABLED_BY_CONFIG,
    REASON_ENABLED,
    REASON_EXCEEDS_MAX_BYTES,
    REASON_EXCEEDS_MAX_PAGES,
    REASON_EXCEEDS_MAX_SHEETS,
    REASON_USER_DISABLED,
    decide_auto_parse,
)


def test_user_disabled():
    d = decide_auto_parse(
        auto_parse_requested=False,
        file_kind="pdf",
        size_bytes=100,
        page_count=1,
        settings=Settings(),
    )
    assert d.should_parse is False
    assert d.reason == REASON_USER_DISABLED


def test_pdf_within_limits():
    settings = Settings(
        auto_parse_pdf_enabled=True,
        auto_parse_pdf_max_pages=10,
        auto_parse_pdf_max_bytes=1000,
    )
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="pdf",
        size_bytes=500,
        page_count=3,
        settings=settings,
    )
    assert d.should_parse is True
    assert d.reason == REASON_ENABLED
    assert d.profile == "layout"


def test_pdf_exceeds_pages():
    settings = Settings(auto_parse_pdf_enabled=True, auto_parse_pdf_max_pages=5, auto_parse_pdf_max_bytes=0)
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="pdf",
        size_bytes=100,
        page_count=20,
        settings=settings,
    )
    assert d.should_parse is False
    assert d.reason == REASON_EXCEEDS_MAX_PAGES


def test_pdf_exceeds_bytes():
    settings = Settings(auto_parse_pdf_enabled=True, auto_parse_pdf_max_pages=0, auto_parse_pdf_max_bytes=100)
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="pdf",
        size_bytes=500,
        page_count=1,
        settings=settings,
    )
    assert d.should_parse is False
    assert d.reason == REASON_EXCEEDS_MAX_BYTES


def test_image_enabled_with_layout_profile_by_default():
    # 显式构造，避免 conftest 里 AUTO_PARSE_IMAGE_ENABLED=false 污染
    # 图片自动解析写 layout（引擎为视觉模型），与 Agent 读取路径一致
    settings = Settings(auto_parse_image_enabled=True, auto_parse_image_profile="layout")
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="image",
        size_bytes=100,
        settings=settings,
    )
    assert d.should_parse is True
    assert d.reason == REASON_ENABLED
    assert d.profile == "layout"


def test_image_disabled_by_config():
    settings = Settings(auto_parse_image_enabled=False)
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="image",
        size_bytes=100,
        settings=settings,
    )
    assert d.should_parse is False
    assert d.reason == REASON_DISABLED_BY_CONFIG


def test_audio_enabled_with_layout_by_default():
    settings = Settings(auto_parse_audio_enabled=True, auto_parse_audio_profile="layout")
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="audio",
        size_bytes=1000,
        settings=settings,
    )
    assert d.should_parse is True
    assert d.reason == REASON_ENABLED
    assert d.profile == "layout"


def test_audio_disabled_by_config():
    settings = Settings(auto_parse_audio_enabled=False)
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="audio",
        size_bytes=1000,
        settings=settings,
    )
    assert d.should_parse is False
    assert d.reason == REASON_DISABLED_BY_CONFIG


def test_video_enabled_with_layout_by_default():
    settings = Settings(auto_parse_video_enabled=True, auto_parse_video_profile="layout")
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="video",
        size_bytes=1000,
        settings=settings,
    )
    assert d.should_parse is True
    assert d.reason == REASON_ENABLED
    assert d.profile == "layout"


def test_video_disabled_by_config():
    settings = Settings(auto_parse_video_enabled=False)
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="video",
        size_bytes=1000,
        settings=settings,
    )
    assert d.should_parse is False
    assert d.reason == REASON_DISABLED_BY_CONFIG


def test_spreadsheet_exceeds_sheets():
    settings = Settings(
        auto_parse_spreadsheet_enabled=True,
        auto_parse_spreadsheet_max_sheets=2,
        auto_parse_spreadsheet_max_bytes=0,
    )
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="spreadsheet",
        size_bytes=100,
        sheet_count=10,
        settings=settings,
    )
    assert d.should_parse is False
    assert d.reason == REASON_EXCEEDS_MAX_SHEETS


def test_text_auto_parse_enabled():
    d = decide_auto_parse(
        auto_parse_requested=True,
        file_kind="text",
        size_bytes=50,
        settings=Settings(),
    )
    assert d.should_parse is True
