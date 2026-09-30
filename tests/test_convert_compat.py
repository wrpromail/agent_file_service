"""file_convert 兼容层。"""
from __future__ import annotations

import pytest

from app.parsers.document_rasterize import PageImage, RasterizeResult
from tests.conftest import TEST_HEADERS


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _download(url, settings=None):
    name = url.rsplit("/", 1)[-1] or "note.docx"
    return b"# Hello\nWorld", name, None


def test_convert_office_requires_auth(client):
    test_client, _ = client
    resp = test_client.post(
        "/convert-office",
        json={"file_url": "http://example.com/a.docx", "file_format": "docx"},
    )
    assert resp.status_code == 401


def test_convert_office_and_cache_hit(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr("app.services.convert_service.download_convert_url", _download)
    body = {"file_url": "http://example.com/note.docx", "file_format": "docx"}
    first = test_client.post("/convert-office", headers=TEST_HEADERS, json=body)
    assert first.status_code == 200, first.text
    data = first.json()
    assert "Hello" in data["markdown_content"]
    assert data["file_format"] == "docx"
    assert data["cache_hit"] is False
    assert data["reference_id"]
    second = test_client.post("/convert-office", headers=TEST_HEADERS, json=body)
    assert second.status_code == 200, second.text
    assert second.json()["cache_hit"] is True


def test_convert_doc_forces_doc(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr("app.services.convert_service.download_convert_url", _download)
    resp = test_client.post(
        "/convert-doc",
        headers=TEST_HEADERS,
        json={"file_url": "http://example.com/x.docx", "file_format": "docx"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_format"] == "doc"


def test_generate_excel_legacy_payload(client):
    pytest.importorskip("openpyxl")
    test_client, _ = client
    resp = test_client.post(
        "/generate-excel",
        headers=TEST_HEADERS,
        json={
            "file_name": "report",
            "source": [
                {
                    "sheet_name": "S1",
                    "data": {
                        "columns": [{"key": "a", "name": "A"}],
                        "rows": [{"a": 1}, {"a": 2}],
                    },
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "report.xlsx" in body["file_urls"]
    assert body["file_base64"]
    assert body["task_id"]


def test_convert_markdown_to_pdf_mocked(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        "app.parsers.markdown_pdf.convert_markdown_to_pdf",
        lambda content, settings=None: b"%PDF-1.4 mock",
    )
    resp = test_client.post(
        "/convert",
        headers=TEST_HEADERS,
        json={"markdown_content": "# Hi"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pdf_base64"]
    assert data["pdf_url"].endswith("/download")
    assert data["message"] == "PDF conversion completed"


def test_extract_text_pdf(client, monkeypatch):
    test_client, _ = client

    def fake_dl(url, settings=None):
        return b"%PDF-fake", "a.pdf", "application/pdf"

    monkeypatch.setattr("app.services.convert_service.download_convert_url", fake_dl)
    monkeypatch.setattr("app.services.convert_service._extract_pdf_text", lambda data: "plain pdf")
    resp = test_client.post(
        "/extract-text",
        headers=TEST_HEADERS,
        json={"file_url": "http://example.com/a.pdf", "file_format": "pdf"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text_content"] == "plain pdf"
    assert resp.json()["cache_hit"] is False


def test_convert_to_images_zip_base64(client, monkeypatch):
    pytest.importorskip("PIL")
    test_client, _ = client
    monkeypatch.setattr("app.services.convert_service.download_convert_url", _download)
    raster = RasterizeResult(
        pages=(PageImage(page=1, png_bytes=_png_bytes(), width=2, height=2),),
        source_pdf_path=None,
        total_pages=1,
        truncated=False,
    )
    monkeypatch.setattr(
        "app.parsers.document_rasterize.render_document_to_page_images",
        lambda *a, **k: raster,
    )
    resp = test_client.post(
        "/convert-to-images",
        headers=TEST_HEADERS,
        json={
            "file_url": "http://example.com/a.pdf",
            "file_format": "pdf",
            "delivery_mode": "base64",
            "pack_mode": "zip",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["page_count"] == 1
    assert data["archive_base64"]
    assert data["pack_mode"] == "zip"


def test_cache_stats_and_clear(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr("app.services.convert_service.download_convert_url", _download)
    test_client.post(
        "/convert-office",
        headers=TEST_HEADERS,
        json={"file_url": "http://example.com/note.docx", "file_format": "docx"},
    )
    stats = test_client.get("/cache/stats", headers=TEST_HEADERS)
    assert stats.status_code == 200, stats.text
    assert stats.json()["total_cached_files"] >= 1
    cleared = test_client.delete("/cache/clear?days=-1", headers=TEST_HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["deleted_count"] >= 1


def test_private_url_allowed_for_convert(client, monkeypatch):
    test_client, _ = client
    seen = {}

    def capture(url, settings=None):
        seen["url"] = url
        return b"# x", "a.docx", None

    monkeypatch.setattr("app.services.convert_service.download_convert_url", capture)
    resp = test_client.post(
        "/convert-office",
        headers=TEST_HEADERS,
        json={"file_url": "http://127.0.0.1:9000/public/a.docx", "file_format": "docx"},
    )
    assert resp.status_code == 200, resp.text
    assert seen["url"].startswith("http://127.0.0.1")


def test_from_url_still_blocks_private(client):
    test_client, _ = client
    resp = test_client.post(
        "/v1/files/from-url",
        headers=TEST_HEADERS,
        json={"url": "http://127.0.0.1/secret"},
    )
    assert resp.status_code in {400, 403}
