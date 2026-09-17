from __future__ import annotations

from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.parse_result import ParseResult
from app.parsers.classifiers import classify_file
from app.parsers.types import FileClassification, FileKind


def classification_from_summary(summary: dict) -> FileClassification | None:
    kind = summary.get("file_kind")
    subtype = summary.get("file_subtype")
    mime_type = summary.get("mime_type", "application/octet-stream")
    if not kind or not subtype:
        return None
    try:
        return FileClassification(kind=FileKind(kind), subtype=subtype, mime_type=mime_type)
    except ValueError:
        return None


def load_classification(
    db: Session,
    *,
    content_hash: str,
    filename: str,
    mime_type: str,
    file_data: BinaryIO | bytes,
) -> FileClassification:
    metadata_row = db.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == content_hash,
            ParseResult.profile == "metadata",
        )
    )
    if metadata_row and metadata_row.summary:
        parsed = classification_from_summary(metadata_row.summary)
        if parsed is not None:
            return parsed
    return classify_file(filename, mime_type, file_data)