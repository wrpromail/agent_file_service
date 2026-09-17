from app.parsers.structure_blocks import split_markdown_to_blocks


def test_split_spreadsheet_sheets():
    md = "## Sheet1\n| a | b |\n\n## Sheet2\n| c |\n"
    blocks, pages = split_markdown_to_blocks(
        md,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="t.xlsx",
    )
    assert pages == 2
    assert len(blocks) == 2
    assert blocks[0].metadata.get("sheet_name") == "Sheet1"
    assert blocks[1].metadata.get("sheet_name") == "Sheet2"


def test_split_by_headings():
    md = "# Intro\nhello\n\n## Body\nworld\n"
    blocks, pages = split_markdown_to_blocks(md, mime_type="text/plain", filename="a.md")
    assert len(blocks) >= 2
    assert pages >= 2


def test_split_form_feed():
    md = "page one\fpage two"
    blocks, pages = split_markdown_to_blocks(md, mime_type="application/pdf", filename="a.pdf")
    assert len(blocks) == 2
    assert pages == 2


def test_monoblock_fallback():
    md = "just a line"
    blocks, pages = split_markdown_to_blocks(md, mime_type="text/plain", filename="a.txt")
    assert len(blocks) == 1
    assert pages == 1
