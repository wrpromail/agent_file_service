from typing import BinaryIO

from app.parsers.base import MetadataExtractor, Parser
from app.parsers.factory import get_processing_factory
from app.parsers.metadata import MetadataParser
from app.parsers.types import FileClassification


class ParserRegistry:
    def __init__(self) -> None:
        self._metadata_extractor: MetadataExtractor = MetadataParser()

    def get_metadata_extractor(self) -> MetadataExtractor:
        return self._metadata_extractor

    def classify(self, filename: str, mime_type: str, file_data: BinaryIO | bytes) -> FileClassification:
        return get_processing_factory().classify(filename, mime_type, file_data)

    def get_parser(
        self,
        mime_type: str,
        profile: str,
        *,
        classification: FileClassification | None = None,
        filename: str = "",
        file_data: BinaryIO | bytes = b"",
    ) -> Parser | None:
        factory = get_processing_factory()
        resolved = classification or factory.classify(filename, mime_type, file_data)
        return factory.resolve_parser(resolved, profile)

    def get_parser_for_classification(
        self,
        classification: FileClassification,
        profile: str,
    ) -> Parser | None:
        return get_processing_factory().resolve_parser(classification, profile)


_registry: ParserRegistry | None = None


def get_registry() -> ParserRegistry:
    global _registry
    if _registry is None:
        _registry = ParserRegistry()
    return _registry


def set_registry(registry: ParserRegistry | None) -> None:
    global _registry
    _registry = registry