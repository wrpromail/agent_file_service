"""vision-pages 正文识别：栅格化 + 多模态 parser + 工厂路由。"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pypdf import PdfWriter

from app.config import Settings
from app.integrations.litellm.client import VisionAnalysisResult
from app.parsers.base import ParseContext
from app.parsers.classifiers import classify_file
from app.parsers.document_rasterize import (
    is_pdf_path,
    render_document_to_page_images,
    render_pdf_to_page_images,
)
from app.parsers.factory import create_processing_factory
from app.parsers.types import FileKind, ProcessingLevel
from app.parsers.vision_pages_layout import VisionPagesLayoutParser


def _blank_pdf_bytes(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_is_pdf_path_by_magic(tmp_path: Path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"%PDF-1.4\n%")
    assert is_pdf_path(p, filename="x.bin", mime_type="") is True


def test_render_pdf_to_page_images_blank(tmp_path: Path):
    pdf = tmp_path / "blank.pdf"
    pdf.write_bytes(_blank_pdf_bytes(2))
    pages, total, truncated = render_pdf_to_page_images(
        pdf, max_pages=1, scale=1.0, max_edge_px=400
    )
    assert total == 2
    assert truncated is True
    assert len(pages) == 1
    assert pages[0].page == 1
    assert pages[0].png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert pages[0].width > 0 and pages[0].height > 0


def test_render_document_pdf_entry(tmp_path: Path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_blank_pdf_bytes(1))
    result = render_document_to_page_images(
        pdf,
        filename="doc.pdf",
        mime_type="application/pdf",
        max_pages=5,
        scale=1.0,
        max_edge_px=400,
        work_dir=tmp_path / "work",
        settings=Settings(vision_pages_work_dir=str(tmp_path / "work")),
    )
    assert len(result.pages) == 1
    assert result.via_libreoffice is False
    assert result.total_pages == 1


def test_vision_pages_parser_mock_client(tmp_path: Path):
    pdf = tmp_path / "contract.pdf"
    pdf.write_bytes(_blank_pdf_bytes(2))

    mock_client = MagicMock()
    mock_client.analyze_image.side_effect = [
        VisionAnalysisResult(markdown="# 第一页\n\n合同标题"),
        VisionAnalysisResult(markdown="## 条款\n\n- 一条"),
    ]

    settings = Settings(
        vision_pages_model="kimi-k2.6:cloud",
        vision_pages_max_pages=10,
        vision_pages_render_scale=1.0,
        vision_pages_max_edge_px=400,
        vision_pages_work_dir=str(tmp_path / "vp"),
        vision_pages_fail_fast=True,
        vision_pages_concurrency=1,
    )
    parser = VisionPagesLayoutParser(client=mock_client, settings=settings)
    ctx = ParseContext(
        content_hash="hash-vp-1",
        mime_type="application/pdf",
        filename="contract.pdf",
        size_bytes=pdf.stat().st_size,
        classification=None,
    )
    structured = parser.parse(str(pdf), ctx)

    assert structured.source.parser_name == "vision-pages"
    assert structured.source.profile == "layout"
    md = structured.metadata.get("markdown") or ""
    assert "第一页" in md
    assert "条款" in md
    assert "<!-- page 1 -->" in md
    assert "<!-- page 2 -->" in md
    assert len(structured.blocks) == 2
    assert structured.document.page_count == 2
    assert mock_client.analyze_image.call_count == 2
    # model override passed
    call_kwargs = mock_client.analyze_image.call_args_list[0].kwargs
    assert call_kwargs.get("model") == "kimi-k2.6:cloud"
    assert structured.metadata.get("concurrency") == 1


def test_vision_pages_concurrency_preserves_page_order(tmp_path: Path):
    """并发完成顺序乱序时，拼接仍按 page 1..N。"""
    import time

    pdf = tmp_path / "multi.pdf"
    pdf.write_bytes(_blank_pdf_bytes(3))

    def _analyze(image_bytes, **kwargs):
        # 后页更快返回，制造乱序
        fname = kwargs.get("filename") or ""
        if "_p1" in fname:
            time.sleep(0.08)
            return VisionAnalysisResult(markdown="PAGE1")
        if "_p2" in fname:
            time.sleep(0.04)
            return VisionAnalysisResult(markdown="PAGE2")
        return VisionAnalysisResult(markdown="PAGE3")

    mock_client = MagicMock()
    mock_client.analyze_image.side_effect = _analyze

    settings = Settings(
        vision_pages_model="kimi-k2.6:cloud",
        vision_pages_max_pages=10,
        vision_pages_render_scale=1.0,
        vision_pages_max_edge_px=400,
        vision_pages_work_dir=str(tmp_path / "vp"),
        vision_pages_concurrency=3,
        vision_pages_fail_fast=True,
    )
    parser = VisionPagesLayoutParser(client=mock_client, settings=settings)
    structured = parser.parse(
        str(pdf),
        ParseContext(
            content_hash="hash-conc",
            mime_type="application/pdf",
            filename="multi.pdf",
            size_bytes=10,
            classification=None,
        ),
    )
    md = structured.metadata.get("markdown") or ""
    pos1, pos2, pos3 = md.find("PAGE1"), md.find("PAGE2"), md.find("PAGE3")
    assert 0 <= pos1 < pos2 < pos3
    assert structured.blocks[0].page == 1
    assert structured.blocks[1].page == 2
    assert structured.blocks[2].page == 3
    assert structured.metadata.get("concurrency") == 3
    assert mock_client.analyze_image.call_count == 3


def test_vision_pages_page_failure_soft(tmp_path: Path):
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(_blank_pdf_bytes(1))
    mock_client = MagicMock()
    mock_client.analyze_image.side_effect = RuntimeError("upstream 500")

    settings = Settings(
        vision_pages_fail_fast=False,
        vision_pages_render_scale=1.0,
        vision_pages_max_edge_px=400,
        vision_pages_work_dir=str(tmp_path / "vp"),
    )
    parser = VisionPagesLayoutParser(client=mock_client, settings=settings)
    structured = parser.parse(
        str(pdf),
        ParseContext(
            content_hash="h2",
            mime_type="application/pdf",
            filename="one.pdf",
            size_bytes=10,
            classification=None,
        ),
    )
    assert "识别失败" in (structured.metadata.get("markdown") or "")
    assert any(w.type == "parse_partial" for w in structured.warnings)


def test_factory_vision_pages_routes_pdf_not_text():
    factory = create_processing_factory(Settings(layout_parser_name="vision-pages"))
    pdf_c = classify_file("a.pdf", "application/pdf", b"%PDF-1.4\n")
    text_c = classify_file("a.txt", "text/plain", b"hello")

    pdf_parser = factory.resolve_parser(pdf_c, ProcessingLevel.LAYOUT.value)
    text_parser = factory.resolve_parser(text_c, ProcessingLevel.LAYOUT.value)
    assert pdf_parser is not None
    assert pdf_parser.name == "vision-pages"
    assert text_parser is not None
    assert text_parser.name == "markitdown"

    office_c = classify_file(
        "a.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"PK",
    )
    assert office_c.kind == FileKind.OFFICE
    office_parser = factory.resolve_parser(office_c, ProcessingLevel.LAYOUT.value)
    assert office_parser is not None
    assert office_parser.name == "vision-pages"

    sheet_c = classify_file(
        "a.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        b"PK",
    )
    assert sheet_c.kind == FileKind.SPREADSHEET
    sheet_parser = factory.resolve_parser(sheet_c, ProcessingLevel.LAYOUT.value)
    assert sheet_parser is not None
    assert sheet_parser.name == "vision-pages"


def test_build_layout_parser_rejects_unknown():
    from app.parsers.factory import _build_layout_parser

    with pytest.raises(ValueError, match="vision-pages"):
        _build_layout_parser(Settings(layout_parser_name="nope"))


def test_office_rasterize_uses_libreoffice_mock(tmp_path: Path):
    """Office 路径：mock LO→PDF，再栅格化。"""
    docx = tmp_path / "memo.docx"
    docx.write_bytes(b"PK\x03\x04fake")
    fake_pdf = tmp_path / "memo.pdf"
    fake_pdf.write_bytes(_blank_pdf_bytes(1))

    with patch(
        "app.parsers.document_rasterize.convert_office_to_pdf",
        return_value=fake_pdf,
    ) as mock_lo:
        result = render_document_to_page_images(
            docx,
            filename="memo.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            max_pages=5,
            scale=1.0,
            max_edge_px=400,
            work_dir=tmp_path / "work",
            settings=Settings(vision_pages_work_dir=str(tmp_path / "work")),
        )
    assert mock_lo.called
    assert result.via_libreoffice is True
    assert len(result.pages) == 1
