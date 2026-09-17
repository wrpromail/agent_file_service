from app.config import Settings
from app.parsers.classifiers import classify_file
from app.parsers.factory import create_processing_factory
from app.parsers.types import FileKind, ProcessingLevel


def test_classify_text_file():
    classification = classify_file("notes.txt", "text/plain", b"hello\nworld")
    assert classification.kind == FileKind.TEXT
    assert classification.subtype == "plain"


def test_classify_wav():
    c = classify_file("a.wav", "audio/wav", b"RIFF")
    assert c.kind == FileKind.AUDIO
    assert c.subtype == "wav"


def test_classify_mp3_by_extension():
    c = classify_file("song.mp3", "", b"ID3")
    assert c.kind == FileKind.AUDIO
    assert c.subtype == "mp3"


def test_classify_mp4_video():
    c = classify_file("clip.mp4", "video/mp4", b"\x00\x00\x00\x18ftyp")
    assert c.kind == FileKind.VIDEO
    assert c.subtype == "mp4"
    assert c.mime_type == "video/mp4"


def test_classify_mov_by_extension():
    c = classify_file("scene.mov", "", b"moov")
    assert c.kind == FileKind.VIDEO
    assert c.subtype == "mov"
    assert c.mime_type == "video/quicktime"


def test_factory_routes_digital_pdf_to_layout():
    factory = create_processing_factory(Settings(layout_parser_name="simple"))
    classification = classify_file("contract.pdf", "application/pdf", b"%PDF-1.4\n")
    routes = factory.build_routes(classification)
    levels = [route.level for route in routes]
    assert ProcessingLevel.METADATA in levels
    assert ProcessingLevel.LAYOUT in levels
    metadata = factory.resolve_metadata_extractor(classification)
    assert metadata.name in {"pdf-digital-metadata", "metadata"}


def test_factory_routes_scanned_pdf_to_ocr():
    factory = create_processing_factory(Settings(layout_parser_name="simple"))
    classification = classify_file("scan.pdf", "application/pdf", b"%PDF-1.4\n")
    # unknown/scanned without pypdf still gets pdf kind
    assert classification.kind == FileKind.PDF
    parser = factory.resolve_parser(classification, ProcessingLevel.OCR.value)
    assert parser is not None
    assert parser.profile == "layout"


def test_factory_image_document_scan_prefers_ocr_route():
    factory = create_processing_factory(Settings(layout_parser_name="simple"))
    classification = classify_file("page_scan.png", "image/png", b"\x89PNG\r\n")
    assert classification.subtype == "document_scan"
    routes = factory.build_routes(classification)
    assert any(route.level == ProcessingLevel.OCR for route in routes)


def test_factory_resolves_image_deep_parser():
    factory = create_processing_factory(Settings(layout_parser_name="simple"))
    classification = classify_file("photo.jpg", "image/jpeg", b"\xff\xd8\xff")
    parser = factory.resolve_parser(classification, ProcessingLevel.DEEP.value)
    assert parser is not None
    assert parser.profile == "deep"


def test_factory_image_layout_uses_vision():
    """Agent 读 layout；图片 layout 应为视觉解析器而非 markitdown。"""
    factory = create_processing_factory(Settings(layout_parser_name="simple"))
    classification = classify_file("photo.jpg", "image/jpeg", b"\xff\xd8\xff")
    parser = factory.resolve_parser(classification, ProcessingLevel.LAYOUT.value)
    assert parser is not None
    assert parser.profile == "layout"
    assert "vision" in parser.name or parser.name == "qwen-vision"
