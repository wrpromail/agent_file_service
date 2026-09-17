from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from app.parsers.types import FileClassification


@dataclass
class ParseContext:
    content_hash: str
    mime_type: str
    filename: str
    size_bytes: int
    classification: FileClassification | None = None


@dataclass
class MetadataResult:
    mime_type: str
    size_bytes: int
    extra: dict = field(default_factory=dict)


class Parser(ABC):
    name: str
    version: str
    supported_mime_types: list[str]
    profile: str

    @abstractmethod
    def can_handle(self, mime_type: str) -> bool: ...

    @abstractmethod
    def parse(self, file_path: str, context: ParseContext):
        """Return StructuredContent defined in app.schemas.content."""
        ...


class MetadataExtractor(ABC):
    name: str = "metadata"
    version: str = "0.1.0"
    profile: str = "metadata"

    @abstractmethod
    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult: ...