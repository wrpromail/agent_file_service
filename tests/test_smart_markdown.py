"""智能模式：过滤 Excel 空 sheet 等噪音。"""
from __future__ import annotations

from pathlib import Path

from app.parsers.base import ParseContext
from app.parsers.markitdown_layout import MarkItDownLayoutParser
from app.parsers.smart_markdown import (
    apply_smart_markdown,
    apply_smart_mode_to_content,
    filter_empty_spreadsheet_sheets,
    is_spreadsheet,
)
from app.schemas.content import ContentBlock, ContentSource, DocumentInfo, StructuredContent
from tests.conftest import install_fake_markitdown, reload_parsers, upload_bytes


# MarkItDown 对空白 sheet 的真实形态（见 sample/outbound.xls）
NOISY_EXCEL_MD = """## Sheet1
| 编号 | 名称 |
| --- | --- |
| 1 | 系统A |

## Sheet2
|
|  |

## Sheet3
|
|  |
"""


def test_is_spreadsheet_by_mime_and_filename():
    assert is_spreadsheet(mime_type="application/vnd.ms-excel")
    assert is_spreadsheet(
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert is_spreadsheet(filename="data.xlsx")
    assert is_spreadsheet(filename="report.XLS")
    assert not is_spreadsheet(mime_type="application/pdf", filename="a.pdf")


def test_filter_empty_sheets_removes_markitdown_noise():
    cleaned = filter_empty_spreadsheet_sheets(NOISY_EXCEL_MD)
    assert "## Sheet1" in cleaned
    assert "系统A" in cleaned
    assert "## Sheet2" not in cleaned
    assert "## Sheet3" not in cleaned


def test_filter_keeps_sheet_with_real_cells():
    md = """## 明细
| id | name |
| --- | --- |
| 1 | a |

## 汇总
| total |
| --- |
| 9 |
"""
    cleaned = filter_empty_spreadsheet_sheets(md)
    assert "## 明细" in cleaned
    assert "## 汇总" in cleaned


def test_filter_drops_pipe_only_variants():
    md = """## Sheet1
| a |
| --- |
| 1 |

## Sheet2
| | |

## Sheet3
|  |  |
| --- | --- |
"""
    cleaned = filter_empty_spreadsheet_sheets(md)
    assert "## Sheet1" in cleaned
    assert "## Sheet2" not in cleaned
    assert "## Sheet3" not in cleaned


def test_apply_smart_markdown_respects_flag_and_type():
    assert apply_smart_markdown(NOISY_EXCEL_MD, smart_mode=False, mime_type="application/vnd.ms-excel") == NOISY_EXCEL_MD
    cleaned = apply_smart_markdown(
        NOISY_EXCEL_MD,
        smart_mode=True,
        mime_type="application/vnd.ms-excel",
        filename="a.xls",
    )
    assert "## Sheet2" not in cleaned
    # 非表格不处理
    assert apply_smart_markdown(NOISY_EXCEL_MD, smart_mode=True, mime_type="text/plain") == NOISY_EXCEL_MD


def test_apply_smart_mode_to_content_updates_block_and_metadata():
    content = StructuredContent(
        content_hash="h1",
        source=ContentSource(parser_name="markitdown", parser_version="0.1.0", profile="layout"),
        document=DocumentInfo(page_count=1),
        blocks=[
            ContentBlock(
                id="md-001",
                type="paragraph",
                page=1,
                text=NOISY_EXCEL_MD,
                metadata={"format": "markdown"},
            )
        ],
        metadata={
            "format": "markdown",
            "markdown": NOISY_EXCEL_MD,
            "char_count": len(NOISY_EXCEL_MD),
            "mime_type": "application/vnd.ms-excel",
            "filename": "outbound.xls",
        },
    )
    out = apply_smart_mode_to_content(content, smart_mode=True)
    assert "## Sheet2" not in out.metadata["markdown"]
    assert out.blocks[0].text == out.metadata["markdown"]
    assert out.metadata["smart_mode"] is True
    assert out.metadata.get("smart_dropped_sheets") == 2
    assert out.metadata["char_count"] == len(out.metadata["markdown"])

    raw = apply_smart_mode_to_content(content, smart_mode=False)
    assert "## Sheet2" in raw.metadata["markdown"]


def test_content_api_smart_mode_default_filters_empty_sheets(client, auth_headers, monkeypatch):
    install_fake_markitdown(monkeypatch, text=NOISY_EXCEL_MD)
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
    reference_id = upload.json()["reference_id"]

    # 默认 smart_mode=true
    content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    )
    assert content.status_code == 200
    payload = content.json()
    md = payload["metadata"]["markdown"]
    assert "## Sheet1" in md
    assert "系统A" in md
    assert "## Sheet2" not in md
    assert "## Sheet3" not in md
    assert payload["metadata"]["smart_mode"] is True

    # 显式关闭：保留原始空 sheet
    raw = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout&smart_mode=false",
        headers=auth_headers,
    )
    assert raw.status_code == 200
    raw_md = raw.json()["metadata"]["markdown"]
    assert "## Sheet2" in raw_md
    assert "## Sheet3" in raw_md


def test_markitdown_parser_still_stores_raw_including_empty_sheets(monkeypatch, tmp_path: Path):
    """解析缓存保留原始 Markdown；智能过滤在 content 读取时做。"""
    sample = tmp_path / "table.xls"
    sample.write_bytes(b"fake")

    install_fake_markitdown(monkeypatch, text=NOISY_EXCEL_MD)

    content = MarkItDownLayoutParser().parse(
        str(sample),
        ParseContext(
            content_hash="abc",
            mime_type="application/vnd.ms-excel",
            filename="outbound.xls",
            size_bytes=4,
        ),
    )
    assert "## Sheet2" in content.metadata["markdown"]
