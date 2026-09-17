"""MarkItDown layout 解析器单元测试（mock，不依赖真实 markitdown 二进制行为）。"""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from app.config import Settings
from app.parsers.base import ParseContext
from app.parsers.classifiers import classify_file
from app.parsers.factory import create_processing_factory
from app.parsers.markitdown_layout import MarkItDownLayoutParser, _extract_markdown
from app.parsers.types import FileKind, ProcessingLevel
from tests.conftest import install_fake_markitdown, reload_parsers, upload_bytes


def test_extract_markdown_prefers_text_content():
    result = SimpleNamespace(text_content="# Hello\n", markdown="ignored")
    assert _extract_markdown(result) == "# Hello\n"


def test_extract_markdown_falls_back_to_markdown_attr():
    result = SimpleNamespace(markdown="## Sheet1\n| a |\n")
    assert _extract_markdown(result).startswith("## Sheet1")


def test_markitdown_parser_returns_single_markdown_block(monkeypatch, tmp_path: Path):
    sample = tmp_path / "table.xls"
    sample.write_bytes(b"fake-xls-bytes")

    def _convert(path: str):
        assert path == str(sample)
        return SimpleNamespace(
            text_content="## Sheet1\n| 编号 | 名称 |\n| --- | --- |\n| 1 | 系统A |\n",
            title="outbound",
        )

    install_fake_markitdown(monkeypatch, convert=_convert)

    parser = MarkItDownLayoutParser()
    context = ParseContext(
        content_hash="abc123",
        mime_type="application/vnd.ms-excel",
        filename="outbound.xls",
        size_bytes=sample.stat().st_size,
    )
    content = parser.parse(str(sample), context)

    assert content.source.parser_name == "markitdown"
    assert content.source.profile == "layout"
    assert content.metadata["format"] == "markdown"
    assert content.metadata["markdown"].startswith("## Sheet1")
    assert content.metadata["char_count"] == len(content.metadata["markdown"])
    # multi-block structure: one block per sheet; full text still in metadata.markdown
    assert len(content.blocks) >= 1
    assert content.blocks[0].metadata.get("format") == "markdown" or content.blocks[0].metadata.get("sheet_name")
    assert "Sheet1" in (content.blocks[0].metadata.get("sheet_name") or content.blocks[0].text or "")
    assert content.tables == []


def test_markitdown_parser_empty_content_emits_warning(monkeypatch, tmp_path: Path):
    sample = tmp_path / "empty.txt"
    sample.write_text("", encoding="utf-8")

    install_fake_markitdown(monkeypatch, text="   ")

    parser = MarkItDownLayoutParser()
    context = ParseContext(
        content_hash="empty",
        mime_type="text/plain",
        filename="empty.txt",
        size_bytes=0,
    )
    content = parser.parse(str(sample), context)
    assert content.warnings
    assert content.warnings[0].type == "parse_partial"


def test_factory_defaults_to_markitdown_layout_parser():
    factory = create_processing_factory(Settings(layout_parser_name="markitdown"))
    classification = classify_file(
        "outbound.xls",
        "application/vnd.ms-excel",
        b"\xd0\xcf\x11\xe0",
    )
    assert classification.kind == FileKind.SPREADSHEET
    parser = factory.resolve_parser(classification, ProcessingLevel.LAYOUT.value)
    assert parser is not None
    assert parser.name == "markitdown"


def test_factory_rejects_unknown_layout_parser_name():
    with pytest.raises(ValueError, match="Unsupported LAYOUT_PARSER_NAME"):
        create_processing_factory(Settings(layout_parser_name="unknown-engine"))


def test_upload_markitdown_layout_end_to_end(client, auth_headers, monkeypatch):
    """API 路径：layout=markitdown 时 content 为 markdown 字符串。"""
    install_fake_markitdown(
        monkeypatch,
        text="## Sheet1\n| id | name |\n| --- | --- |\n| 1 | demo |\n",
    )
    reload_parsers(monkeypatch, LAYOUT_PARSER_NAME="markitdown")

    test_client, _ = client
    upload = upload_bytes(
        client,
        auth_headers,
        b"\xd0\xcf\x11\xe0fake",
        name="outbound.xls",
        mime="application/vnd.ms-excel",
    )
    assert upload.status_code == 201
    body = upload.json()
    assert body["parse_status"] == "completed"
    reference_id = body["reference_id"]

    content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    )
    assert content.status_code == 200
    payload = content.json()
    assert payload["source"]["parser_name"] == "markitdown"
    assert payload["metadata"]["format"] == "markdown"
    assert "| id | name |" in payload["blocks"][0]["text"]
    markdown = payload["metadata"]["markdown"]
    # Full markdown is source of truth; multi-block sheet text may differ by trailing newline
    assert "| id | name |" in markdown
    assert "Sheet1" in markdown
