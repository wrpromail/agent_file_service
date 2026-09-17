from __future__ import annotations

from typing import BinaryIO

from app.config import Settings, get_settings
from app.parsers.base import MetadataExtractor, Parser
from app.parsers.classifiers import classify_file
from app.parsers.metadata_handlers import (
    AudioMetadataExtractor,
    GenericMetadataExtractor,
    ImageMetadataExtractor,
    OfficeMetadataExtractor,
    PdfDigitalMetadataExtractor,
    PdfScannedMetadataExtractor,
    SpreadsheetMetadataExtractor,
    TextMetadataExtractor,
    VideoMetadataExtractor,
)
from app.parsers.types import (
    WILDCARD,
    FileClassification,
    FileKind,
    ProcessingLevel,
    ProcessingRoute,
)


class ProcessingFactory:
    """按文件类型/子类型 + 处理层级解析处理器。"""

    def __init__(self) -> None:
        self._metadata: dict[tuple[str, str], MetadataExtractor] = {}
        self._parsers: dict[tuple[str, str, str], Parser] = {}
        self._default_levels: dict[tuple[str, str], list[ProcessingLevel]] = {}

    def register_metadata(
        self,
        kind: str,
        subtype: str,
        extractor: MetadataExtractor,
    ) -> None:
        self._metadata[(kind, subtype)] = extractor

    def register_parser(
        self,
        kind: str,
        subtype: str,
        profile: str,
        parser: Parser,
    ) -> None:
        self._parsers[(kind, subtype, profile)] = parser

    def register_default_levels(
        self,
        kind: str,
        subtype: str,
        levels: list[ProcessingLevel],
    ) -> None:
        self._default_levels[(kind, subtype)] = levels

    def classify(self, filename: str, mime_type: str, file_data: BinaryIO | bytes) -> FileClassification:
        return classify_file(filename, mime_type, file_data)

    def resolve_metadata_extractor(self, classification: FileClassification) -> MetadataExtractor:
        return self._resolve(self._metadata, classification, fallback_key=WILDCARD)

    def resolve_parser(self, classification: FileClassification, profile: str) -> Parser | None:
        key = (classification.kind.value, classification.subtype, profile)
        if key in self._parsers:
            return self._parsers[key]
        wildcard_subtype = (classification.kind.value, WILDCARD, profile)
        if wildcard_subtype in self._parsers:
            return self._parsers[wildcard_subtype]
        global_key = (WILDCARD, WILDCARD, profile)
        return self._parsers.get(global_key)

    def default_levels(self, classification: FileClassification) -> list[ProcessingLevel]:
        exact = self._default_levels.get(classification.key())
        if exact is not None:
            return exact
        by_kind = self._default_levels.get((classification.kind.value, WILDCARD))
        if by_kind is not None:
            return by_kind
        return self._default_levels.get(
            (WILDCARD, WILDCARD),
            [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT],
        )

    def build_routes(self, classification: FileClassification) -> list[ProcessingRoute]:
        routes: list[ProcessingRoute] = []
        metadata_extractor = self.resolve_metadata_extractor(classification)
        routes.append(
            ProcessingRoute(
                level=ProcessingLevel.METADATA,
                handler_name=metadata_extractor.name,
                handler_version=metadata_extractor.version,
                sync=True,
                description="Fast metadata extraction",
            )
        )
        for level in self.default_levels(classification):
            if level == ProcessingLevel.METADATA:
                continue
            parser = self.resolve_parser(classification, level.value)
            if parser is None:
                continue
            routes.append(
                ProcessingRoute(
                    level=level,
                    handler_name=parser.name,
                    handler_version=parser.version,
                    sync=False,
                    description=f"{level.value} processing",
                )
            )
        return routes

    @staticmethod
    def _resolve(mapping: dict, classification: FileClassification, *, fallback_key: str):
        kind, subtype = classification.key()
        if (kind, subtype) in mapping:
            return mapping[(kind, subtype)]
        if (kind, fallback_key) in mapping:
            return mapping[(kind, fallback_key)]
        if (WILDCARD, fallback_key) in mapping:
            return mapping[(WILDCARD, fallback_key)]
        raise KeyError(f"No handler registered for {classification}")


_factory: ProcessingFactory | None = None


def _build_layout_parser(settings: Settings) -> Parser:
    """按 ``layout_parser_name`` 选择**正文识别**后端（结果写入 profile=layout）。

    API 缓存槽位名仍是 ``layout``（历史兼容 = 基础正文 Markdown），
    这里选的是「如何识别正文」的引擎，不是狭义的版面分析产品能力。

    - markitdown（默认）：确定性文件 → Markdown，适合智能体直接消费
    - simple：轻量文本行切分，仅测试 / 无外部依赖场景
    - mineru：外部 MinerU API，高精度文档解析（亦作正文识别后端之一）
    - vision-pages：页图 + LiteLLM 多模态正文识别（PDF/Office/表格）
    """
    name = (settings.layout_parser_name or "markitdown").strip().lower()
    if name == "mineru":
        from app.parsers.mineru_layout import MinerULayoutParser

        return MinerULayoutParser()
    if name == "simple":
        from app.parsers.simple_layout import SimpleLayoutParser

        return SimpleLayoutParser()
    if name in {"vision-pages", "vision_pages", "page-vision"}:
        from app.parsers.vision_pages_layout import VisionPagesLayoutParser

        return VisionPagesLayoutParser(settings=settings)
    if name == "markitdown":
        from app.parsers.markitdown_layout import MarkItDownLayoutParser

        return MarkItDownLayoutParser()
    raise ValueError(
        f"Unsupported LAYOUT_PARSER_NAME={settings.layout_parser_name!r}; "
        "expected one of: markitdown, simple, mineru, vision-pages "
        "(body/content recognition backends for profile=layout)"
    )


def create_processing_factory(settings: Settings | None = None) -> ProcessingFactory:
    """工厂函数：注册各类型/子类型在不同层级的处理器。"""
    settings = settings or get_settings()
    factory = ProcessingFactory()

    generic_metadata = GenericMetadataExtractor()
    factory.register_metadata(WILDCARD, WILDCARD, generic_metadata)

    factory.register_metadata(FileKind.PDF.value, "digital", PdfDigitalMetadataExtractor())
    factory.register_metadata(FileKind.PDF.value, "scanned", PdfScannedMetadataExtractor())
    factory.register_metadata(FileKind.PDF.value, WILDCARD, PdfDigitalMetadataExtractor())

    factory.register_metadata(FileKind.SPREADSHEET.value, WILDCARD, SpreadsheetMetadataExtractor())
    factory.register_metadata(FileKind.OFFICE.value, WILDCARD, OfficeMetadataExtractor())
    factory.register_metadata(FileKind.TEXT.value, WILDCARD, TextMetadataExtractor())
    factory.register_metadata(FileKind.IMAGE.value, WILDCARD, ImageMetadataExtractor())
    factory.register_metadata(FileKind.AUDIO.value, WILDCARD, AudioMetadataExtractor())
    factory.register_metadata(FileKind.VIDEO.value, WILDCARD, VideoMetadataExtractor())

    # 正文识别后端（配置名 layout_parser_name 为历史字段；写入 profile=layout）
    body_engine = (settings.layout_parser_name or "markitdown").strip().lower()
    body_parser = _build_layout_parser(settings)
    layout_parser = body_parser  # 兼容下文变量名：注册到 profile=layout
    ocr_parser = layout_parser

    # vision-pages 正文识别仅覆盖 PDF/Office/表格；文本与 wildcard 回退 markitdown，避免烧视觉
    text_layout_parser = layout_parser
    wildcard_layout_parser = layout_parser
    if body_engine in {"vision-pages", "vision_pages", "page-vision"}:
        from app.parsers.markitdown_layout import MarkItDownLayoutParser

        text_layout_parser = MarkItDownLayoutParser()
        wildcard_layout_parser = text_layout_parser

    factory.register_parser(WILDCARD, WILDCARD, ProcessingLevel.LAYOUT.value, wildcard_layout_parser)
    factory.register_parser(WILDCARD, WILDCARD, ProcessingLevel.OCR.value, ocr_parser)

    # pdf/* 含 digital / scanned / unknown（最小 PDF 探测失败时 subtype=unknown）
    factory.register_parser(FileKind.PDF.value, WILDCARD, ProcessingLevel.LAYOUT.value, layout_parser)
    factory.register_parser(FileKind.PDF.value, WILDCARD, ProcessingLevel.OCR.value, ocr_parser)
    factory.register_parser(FileKind.PDF.value, "digital", ProcessingLevel.LAYOUT.value, layout_parser)
    factory.register_parser(FileKind.PDF.value, "scanned", ProcessingLevel.OCR.value, ocr_parser)
    factory.register_parser(FileKind.PDF.value, "scanned", ProcessingLevel.LAYOUT.value, layout_parser)

    from app.parsers.vision_deep import VisionDeepParser

    # 图片：layout 与 deep 均走视觉识别（常见调用方只拉 profile=layout）
    vision_layout = VisionDeepParser(settings=settings, profile=ProcessingLevel.LAYOUT.value)
    vision_deep = VisionDeepParser(settings=settings, profile=ProcessingLevel.DEEP.value)
    factory.register_parser(FileKind.IMAGE.value, WILDCARD, ProcessingLevel.DEEP.value, vision_deep)
    factory.register_parser(FileKind.IMAGE.value, WILDCARD, ProcessingLevel.LAYOUT.value, vision_layout)
    factory.register_parser(FileKind.IMAGE.value, "document_scan", ProcessingLevel.OCR.value, vision_layout)
    factory.register_parser(FileKind.IMAGE.value, "document_scan", ProcessingLevel.LAYOUT.value, vision_layout)
    factory.register_parser(FileKind.IMAGE.value, "screenshot", ProcessingLevel.LAYOUT.value, vision_layout)
    factory.register_parser(FileKind.IMAGE.value, "photo", ProcessingLevel.LAYOUT.value, vision_layout)

    factory.register_parser(FileKind.SPREADSHEET.value, WILDCARD, ProcessingLevel.LAYOUT.value, layout_parser)
    from app.parsers.spreadsheet_tables import SpreadsheetTablesParser

    tables_parser = SpreadsheetTablesParser(
        max_sheets=int(getattr(settings, "auto_parse_spreadsheet_max_sheets", 50) or 50),
        max_rows=int(getattr(settings, "auto_parse_spreadsheet_tables_max_rows", 5000) or 5000),
        max_cols=int(getattr(settings, "auto_parse_spreadsheet_tables_max_cols", 64) or 64),
    )
    factory.register_parser(
        FileKind.SPREADSHEET.value,
        WILDCARD,
        ProcessingLevel.TABLES.value,
        tables_parser,
    )
    # Also allow explicit tables profile on generic xlsx mime via kind wildcard
    factory.register_parser(
        WILDCARD,
        WILDCARD,
        ProcessingLevel.TABLES.value,
        tables_parser,
    )
    factory.register_parser(FileKind.OFFICE.value, WILDCARD, ProcessingLevel.LAYOUT.value, layout_parser)
    factory.register_parser(FileKind.TEXT.value, WILDCARD, ProcessingLevel.LAYOUT.value, text_layout_parser)

    from app.parsers.audio_layout import AudioLayoutParser
    from app.parsers.video_layout import VideoLayoutParser

    # 音频 layout：LiteLLM 音频模型转写（调用方通常只拉 layout）
    audio_layout = AudioLayoutParser(settings=settings, profile=ProcessingLevel.LAYOUT.value)
    factory.register_parser(FileKind.AUDIO.value, WILDCARD, ProcessingLevel.LAYOUT.value, audio_layout)

    # 视频 layout：LiteLLM 视频模型理解（调用方通常只拉 layout）
    video_layout = VideoLayoutParser(settings=settings, profile=ProcessingLevel.LAYOUT.value)
    factory.register_parser(FileKind.VIDEO.value, WILDCARD, ProcessingLevel.LAYOUT.value, video_layout)

    factory.register_default_levels(FileKind.PDF.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT])
    factory.register_default_levels(FileKind.PDF.value, "digital", [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT])
    factory.register_default_levels(
        FileKind.PDF.value, "scanned", [ProcessingLevel.METADATA, ProcessingLevel.OCR, ProcessingLevel.LAYOUT]
    )
    factory.register_default_levels(
        FileKind.IMAGE.value,
        "document_scan",
        [ProcessingLevel.METADATA, ProcessingLevel.OCR, ProcessingLevel.LAYOUT],
    )
    factory.register_default_levels(
        FileKind.IMAGE.value, "screenshot", [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT]
    )
    factory.register_default_levels(
        FileKind.IMAGE.value, "photo", [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT]
    )
    factory.register_default_levels(
        FileKind.IMAGE.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT]
    )
    factory.register_default_levels(
        FileKind.SPREADSHEET.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT]
    )
    factory.register_default_levels(
        FileKind.OFFICE.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT]
    )
    factory.register_default_levels(FileKind.TEXT.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT])
    # 音频：metadata + layout（转写）；特定指令仍可 custom-parse
    factory.register_default_levels(
        FileKind.AUDIO.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT]
    )
    # 视频：metadata + layout（理解/转写）；特定指令仍可 custom-parse
    factory.register_default_levels(
        FileKind.VIDEO.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT]
    )
    factory.register_default_levels(FileKind.UNKNOWN.value, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT])
    factory.register_default_levels(WILDCARD, WILDCARD, [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT])

    return factory


def get_processing_factory() -> ProcessingFactory:
    global _factory
    if _factory is None:
        _factory = create_processing_factory()
    return _factory


def set_processing_factory(factory: ProcessingFactory | None) -> None:
    global _factory
    _factory = factory