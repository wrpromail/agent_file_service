from dataclasses import dataclass
from enum import StrEnum


class FileKind(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"
    OFFICE = "office"
    TEXT = "text"
    AUDIO = "audio"
    VIDEO = "video"
    ARCHIVE = "archive"
    UNKNOWN = "unknown"


class ProcessingLevel(StrEnum):
    """处理层级：由快到精。"""

    METADATA = "metadata"
    LAYOUT = "layout"
    OCR = "ocr"
    DEEP = "deep"
    # Spreadsheet structured JSON (headers + rows); independent of layout markdown.
    TABLES = "tables"


WILDCARD = "*"


@dataclass(frozen=True)
class FileClassification:
    kind: FileKind
    subtype: str
    mime_type: str

    def key(self) -> tuple[str, str]:
        return self.kind.value, self.subtype

    def to_dict(self) -> dict[str, str]:
        return {
            "file_kind": self.kind.value,
            "file_subtype": self.subtype,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True)
class ProcessingRoute:
    """某文件分类在指定层级的处理路由。"""

    level: ProcessingLevel
    handler_name: str
    handler_version: str
    sync: bool
    description: str = ""