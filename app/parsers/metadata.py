from typing import BinaryIO

from app.parsers.base import MetadataExtractor, MetadataResult
from app.parsers.factory import get_processing_factory
from app.parsers.types import FileClassification


class MetadataParser(MetadataExtractor):
    """兼容层：委托 ProcessingFactory 按类型/子类型选择元数据提取器。"""

    name = "metadata"
    version = "0.1.0"
    profile = "metadata"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        factory = get_processing_factory()
        resolved = classification or factory.classify(filename, mime_type, file_data)
        extractor = factory.resolve_metadata_extractor(resolved)
        result = extractor.extract(
            file_data,
            filename,
            mime_type,
            classification=resolved,
        )
        routes = factory.build_routes(resolved)
        result.extra.setdefault("file_kind", resolved.kind.value)
        result.extra.setdefault("file_subtype", resolved.subtype)
        result.extra["processing_routes"] = [
            {
                "level": route.level.value,
                "handler_name": route.handler_name,
                "handler_version": route.handler_version,
                "sync": route.sync,
            }
            for route in routes
        ]
        return result