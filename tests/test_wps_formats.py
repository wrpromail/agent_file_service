"""金山 WPS 原生格式：分类、legacy 转换映射、浅元数据、archive 白名单。"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.core.storage import InMemoryStorageBackend, set_storage
from app.parsers.classifiers import classify_file
from app.parsers.legacy_office import (
    detect_legacy_office_format,
    ensure_normalized_office,
    needs_legacy_conversion,
)
from app.parsers.metadata_handlers import OfficeMetadataExtractor, SpreadsheetMetadataExtractor
from app.parsers.types import FileKind
from app.services.archive_service import _default_supported_extensions, _is_supported_file


def test_classify_wps_spreadsheet_et():
    c = classify_file("报表.et", "application/octet-stream", b"\xd0\xcf\x11\xe0")
    assert c.kind == FileKind.SPREADSHEET
    assert c.subtype == "et"
    assert "wps-office.et" in c.mime_type or c.mime_type == "application/wps-office.et"

    c2 = classify_file("tpl.ett", "", b"x")
    assert c2.kind == FileKind.SPREADSHEET
    assert c2.subtype == "ett"


def test_classify_wps_word_and_presentation():
    c = classify_file("通知.wps", "application/octet-stream", b"\xd0\xcf\x11\xe0")
    assert c.kind == FileKind.OFFICE
    assert c.subtype == "wps"
    assert c.mime_type == "application/wps-office.wps"

    c2 = classify_file("tpl.wpt", "", b"x")
    assert c2.subtype == "wpt"

    c3 = classify_file("汇报.dps", "", b"x")
    assert c3.kind == FileKind.OFFICE
    assert c3.subtype == "dps"
    assert c3.mime_type == "application/wps-office.dps"

    c4 = classify_file("tpl.dpt", "application/wps-office.dpt", b"x")
    assert c4.subtype == "dpt"


def test_ms_works_mime_alone_does_not_become_kingsoft_wps():
    """application/vnd.ms-works 是微软 Works，不能仅凭 MIME 判为金山 WPS。"""
    c = classify_file("mystery.bin", "application/vnd.ms-works", b"x")
    assert c.subtype != "wps"
    assert c.kind == FileKind.UNKNOWN or c.subtype != "wps"


def test_detect_legacy_wps_formats():
    assert detect_legacy_office_format("a.et") == "et"
    assert detect_legacy_office_format("a.ett") == "ett"
    assert detect_legacy_office_format("a.wps") == "wps"
    assert detect_legacy_office_format("a.wpt") == "wpt"
    assert detect_legacy_office_format("a.dps") == "dps"
    assert detect_legacy_office_format("a.dpt") == "dpt"
    assert detect_legacy_office_format("x.bin", "application/wps-office.et") == "et"
    assert detect_legacy_office_format("x.bin", "application/vnd.ms-works") is None
    assert needs_legacy_conversion("sheet.ET") is True
    assert needs_legacy_conversion("sheet.xlsx") is False


def test_ensure_normalized_et_to_xlsx(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGACY_OFFICE_ENABLED", "true")
    monkeypatch.setenv("LEGACY_OFFICE_CACHE_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    storage = InMemoryStorageBackend()
    set_storage(storage)

    src = tmp_path / "source.et"
    src.write_bytes(b"fake-et")

    with patch("app.parsers.legacy_office.resolve_soffice_binary", return_value="/usr/bin/soffice"):
        with patch("app.parsers.legacy_office.convert_with_libreoffice") as conv:
            def _fake_convert(input_path, target_format, *, output_dir, binary, timeout=300):
                assert target_format == "xlsx"
                assert Path(input_path).suffix == ".et"
                out = Path(output_dir) / "source.xlsx"
                out.write_bytes(b"fake-xlsx")
                return str(out)

            conv.side_effect = _fake_convert
            result = ensure_normalized_office(
                str(src),
                filename="报表.et",
                mime_type="application/wps-office.et",
                content_hash="abc123" * 8,
                storage=storage,
                settings=settings,
            )
    assert result is not None
    assert result.source_format == "et"
    assert result.target_format == "xlsx"
    assert result.cache_hit is False
    assert Path(result.path).read_bytes() == b"fake-xlsx"
    get_settings.cache_clear()


def test_wps_shallow_metadata():
    sheet = SpreadsheetMetadataExtractor().extract(
        io.BytesIO(b"x"),
        "a.et",
        "application/wps-office.et",
        classification=classify_file("a.et", "", b"x"),
    )
    assert sheet.extra.get("workbook_format") == "et"
    assert "wps_et_limited_metadata" in (sheet.extra.get("extractor_warning") or "")

    office = OfficeMetadataExtractor().extract(
        io.BytesIO(b"x"),
        "a.wps",
        "application/wps-office.wps",
        classification=classify_file("a.wps", "", b"x"),
    )
    assert office.extra.get("office_format") == "wps"
    assert "wps_limited_metadata" in (office.extra.get("extractor_warning") or "")

    dps = OfficeMetadataExtractor().extract(
        io.BytesIO(b"x"),
        "a.dps",
        "application/wps-office.dps",
        classification=classify_file("a.dps", "", b"x"),
    )
    assert dps.extra.get("office_format") == "dps"


def test_archive_whitelist_includes_wps():
    settings = get_settings()
    supported = _default_supported_extensions(settings)
    for ext in (".et", ".ett", ".wps", ".wpt", ".dps", ".dpt"):
        assert ext in supported
        assert _is_supported_file(f"dir/file{ext}", supported)
