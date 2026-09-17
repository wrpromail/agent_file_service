from app.parsers.content_view import (
    apply_char_window,
    apply_content_view,
    extract_outline_from_markdown,
    filter_markdown_by_sheets,
    parse_sheets_param,
)
from app.schemas.content import ContentBlock, ContentSource, DocumentInfo, StructuredContent


def _content(markdown: str, *, mime: str = "text/plain", filename: str = "a.txt") -> StructuredContent:
    return StructuredContent(
        content_hash="abc",
        source=ContentSource(parser_name="t", parser_version="1", profile="layout"),
        document=DocumentInfo(page_count=1),
        blocks=[ContentBlock(id="md-001", type="paragraph", page=1, text=markdown)],
        metadata={"markdown": markdown, "mime_type": mime, "filename": filename},
    )


def test_parse_sheets_param():
    assert parse_sheets_param(None) is None
    assert parse_sheets_param("Sheet1, ## Sheet2") == {"sheet1", "sheet2"}


def test_filter_markdown_by_sheets():
    md = "## Sheet1\n| a |\n\n## Sheet2\n| b |\n"
    out = filter_markdown_by_sheets(md, {"sheet1"})
    assert "Sheet1" in out
    assert "Sheet2" not in out


def test_char_window():
    text, meta = apply_char_window("abcdefghij", offset=2, max_chars=3)
    assert text == "cde"
    assert meta["truncated"] is True
    assert meta["next_offset"] == 5
    assert meta["total_char_count"] == 10


def test_outline_mode():
    md = "# Title\n\npara\n\n## Section\nmore\n"
    result = apply_content_view(_content(md), mode="outline")
    assert result.metadata["mode"] == "outline"
    assert "Title" in result.metadata["markdown"]
    assert result.metadata.get("outline")


def test_max_chars_on_content():
    md = "x" * 100
    result = apply_content_view(_content(md), max_chars=10, offset=0)
    assert result.metadata["markdown"] == "x" * 10
    assert result.metadata["truncated"] is True
    assert result.metadata["next_offset"] == 10


def test_extract_outline():
    items = extract_outline_from_markdown("# A\n\n## B\n")
    assert items[0]["title"] == "A"
    assert items[1]["title"] == "B"
