"""旧版 Office（doc/ppt/xls）规范化与派生产物缓存。"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch


from app.config import Settings, get_settings
from app.core.storage import InMemoryStorageBackend, derivative_storage_key, set_storage
from app.parsers.classifiers import classify_file
from app.parsers.legacy_office import (
    CONVERTER_VERSION,
    convert_with_libreoffice,
    detect_legacy_office_format,
    ensure_normalized_office,
    needs_legacy_conversion,
    purge_legacy_derivatives,
)
from app.parsers.types import FileKind


def test_detect_legacy_formats():
    assert detect_legacy_office_format("a.doc") == "doc"
    assert detect_legacy_office_format("a.ppt") == "ppt"
    assert detect_legacy_office_format("a.xls") == "xls"
    assert detect_legacy_office_format("a.et") == "et"
    assert detect_legacy_office_format("a.wps") == "wps"
    assert detect_legacy_office_format("a.dps") == "dps"
    assert detect_legacy_office_format("a.docx") is None
    assert detect_legacy_office_format("a.pptx") is None
    assert detect_legacy_office_format("a.xlsx") is None
    assert detect_legacy_office_format("x.bin", "application/msword") == "doc"
    assert needs_legacy_conversion("report.PPT") is True
    assert needs_legacy_conversion("report.docx") is False


def test_classify_ppt_as_office():
    c = classify_file("slides.ppt", "application/vnd.ms-powerpoint", b"\xd0\xcf\x11\xe0")
    assert c.kind == FileKind.OFFICE
    assert c.subtype == "ppt"

    c2 = classify_file(
        "slides.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        b"PK",
    )
    assert c2.kind == FileKind.OFFICE
    assert c2.subtype == "pptx"


def test_convert_with_libreoffice_invokes_subprocess(tmp_path):
    src = tmp_path / "source.doc"
    src.write_bytes(b"fake-doc")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # 模拟 soffice 写出目标文件
    expected = out_dir / "source.docx"
    expected.write_bytes(b"fake-docx")

    with patch("app.parsers.legacy_office.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        path = convert_with_libreoffice(
            str(src),
            "docx",
            output_dir=str(out_dir),
            binary="/usr/bin/soffice",
            timeout=30,
        )
    assert path == str(expected)
    assert run.called
    cmd = run.call_args.args[0]
    assert "--convert-to" in cmd
    assert "docx" in cmd


def test_ensure_normalized_caches_derivative(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGACY_OFFICE_ENABLED", "true")
    monkeypatch.setenv("LEGACY_OFFICE_CACHE_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    storage = InMemoryStorageBackend()
    set_storage(storage)

    src = tmp_path / "legacy.doc"
    src.write_bytes(b"OLE-fake")
    content_hash = "a" * 64

    def fake_convert(input_path, target_format, *, output_dir, binary, timeout=300):
        out = Path(output_dir) / f"source.{target_format}"
        out.write_bytes(b"PK-converted-docx")
        return str(out)

    with (
        patch("app.parsers.legacy_office.resolve_soffice_binary", return_value="/bin/soffice"),
        patch("app.parsers.legacy_office.convert_with_libreoffice", side_effect=fake_convert),
    ):
        first = ensure_normalized_office(
            str(src),
            filename="legacy.doc",
            mime_type="application/msword",
            content_hash=content_hash,
            storage=storage,
            settings=settings,
        )
        assert first is not None
        assert first.cache_hit is False
        assert first.source_format == "doc"
        assert first.target_format == "docx"
        assert Path(first.path).read_bytes() == b"PK-converted-docx"

        key = derivative_storage_key(
            content_hash,
            kind="legacy-office",
            version=settings.legacy_office_converter_version,
            filename="normalized.docx",
        )
        assert storage.exists(key)

        second = ensure_normalized_office(
            str(src),
            filename="legacy.doc",
            mime_type="application/msword",
            content_hash=content_hash,
            storage=storage,
            settings=settings,
        )
        assert second is not None
        assert second.cache_hit is True
        assert Path(second.path).read_bytes() == b"PK-converted-docx"

    from app.parsers.legacy_office import cleanup_legacy_normalize

    cleanup_legacy_normalize(first)
    cleanup_legacy_normalize(second)


def test_ensure_normalized_skips_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGACY_OFFICE_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()
    storage = InMemoryStorageBackend()
    src = tmp_path / "legacy.doc"
    src.write_bytes(b"x")
    result = ensure_normalized_office(
        str(src),
        filename="legacy.doc",
        mime_type="application/msword",
        content_hash="b" * 64,
        storage=storage,
        settings=settings,
    )
    assert result is None


def test_ensure_normalized_noop_for_docx(tmp_path):
    storage = InMemoryStorageBackend()
    src = tmp_path / "modern.docx"
    src.write_bytes(b"PK")
    result = ensure_normalized_office(
        str(src),
        filename="modern.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content_hash="c" * 64,
        storage=storage,
        settings=Settings(legacy_office_enabled=True),
    )
    assert result is None


def test_purge_legacy_derivatives():
    storage = InMemoryStorageBackend()
    content_hash = "d" * 64
    key = derivative_storage_key(
        content_hash,
        kind="legacy-office",
        version=CONVERTER_VERSION,
        filename="normalized.docx",
    )
    storage.put_object(key, io.BytesIO(b"x"), "application/octet-stream")
    assert storage.exists(key)
    purge_legacy_derivatives(storage, content_hash, converter_version=CONVERTER_VERSION)
    assert not storage.exists(key)


def test_layout_parse_uses_legacy_normalize(client, auth_headers, monkeypatch):
    """上传 .doc 后 layout 路径应先规范化再解析（mock LibreOffice + parser）。"""
    test_client, _storage = client
    monkeypatch.setenv("LAYOUT_PARSER_NAME", "simple")
    monkeypatch.setenv("LEGACY_OFFICE_ENABLED", "true")
    monkeypatch.setenv("LEGACY_OFFICE_CACHE_ENABLED", "true")
    get_settings.cache_clear()
    from app.parsers.factory import set_processing_factory
    from app.parsers.registry import set_registry
    from app.schemas.content import ContentBlock, ContentSource, DocumentInfo, StructuredContent

    set_processing_factory(None)
    set_registry(None)

    converted_bytes = b"PK\x03\x04-fake-docx"
    seen_paths: list[str] = []

    def fake_convert(input_path, target_format, *, output_dir, binary, timeout=300):
        out = Path(output_dir) / f"source.{target_format}"
        out.write_bytes(converted_bytes)
        return str(out)

    def fake_parse(self, file_path, context):
        seen_paths.append(file_path)
        assert str(file_path).endswith(".docx"), file_path
        assert Path(file_path).read_bytes() == converted_bytes
        return StructuredContent(
            content_hash=context.content_hash,
            source=ContentSource(
                parser_name=self.name,
                parser_version=self.version,
                profile=self.profile,
            ),
            document=DocumentInfo(page_count=1),
            blocks=[
                ContentBlock(
                    id="md-001",
                    type="paragraph",
                    page=1,
                    text="# From legacy DOC\n\nHello SOE.",
                    confidence=1.0,
                )
            ],
            metadata={
                "format": "markdown",
                "markdown": "# From legacy DOC\n\nHello SOE.",
                "char_count": 30,
            },
        )

    with (
        patch("app.parsers.legacy_office.resolve_soffice_binary", return_value="/bin/soffice"),
        patch("app.parsers.legacy_office.convert_with_libreoffice", side_effect=fake_convert),
        patch("app.parsers.simple_layout.SimpleLayoutParser.parse", fake_parse),
    ):
        resp = test_client.post(
            "/v1/files",
            headers=auth_headers,
            files={"file": ("notice.doc", b"\xd0\xcf\x11\xe0fake", "application/msword")},
            data={"auto_parse": "true"},
        )
        assert resp.status_code == 201, resp.text
        ref_id = resp.json()["reference_id"]

        content = test_client.get(
            f"/v1/files/{ref_id}/content",
            headers=auth_headers,
            params={"profile": "layout"},
        )
        assert content.status_code == 200, content.text
        body = content.json()
        assert body.get("ready") is True
        md = body.get("metadata", {}).get("markdown") or ""
        if not md and body.get("blocks"):
            md = body["blocks"][0].get("text") or ""
        assert "Hello SOE" in md
        assert body.get("metadata", {}).get("legacy_converted") is True
        assert body.get("metadata", {}).get("legacy_source_format") == "doc"
        assert seen_paths, "parser should have been invoked on normalized path"
