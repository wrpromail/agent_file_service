"""分层元数据提取器：按文件 kind/subtype 提取廉价、可缓存的事实字段。

设计约定（与 docs/metadata-extraction.md 一致）：
- 只提取结构/技术事实，不做业务语义解读
- 失败降级：解析失败时返回已有基础字段，不抛出阻断上传
- 字段写入 MetadataResult.extra，由 API 映射到 FileMetadata
"""
from __future__ import annotations

import csv
import io
import mimetypes
import re
from pathlib import Path
from typing import Any, BinaryIO

from app.parsers.base import MetadataExtractor, MetadataResult
from app.parsers.types import FileClassification


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------


def _seek_start(file_data: BinaryIO) -> None:
    if hasattr(file_data, "seek"):
        try:
            file_data.seek(0)
        except (io.UnsupportedOperation, OSError):
            pass


def _size_of(file_data: BinaryIO) -> int:
    """Best-effort size of a seekable binary stream without consuming it."""
    if hasattr(file_data, "seek") and hasattr(file_data, "tell"):
        try:
            cur = file_data.tell()
            file_data.seek(0, io.SEEK_END)
            size = file_data.tell()
            file_data.seek(cur, io.SEEK_SET)
            return size
        except (io.UnsupportedOperation, OSError):
            pass
    data = file_data.read() if hasattr(file_data, "read") else b""
    if hasattr(file_data, "seek"):
        _seek_start(file_data)
    return len(data)


def _classification_extra(classification: FileClassification | None) -> dict[str, Any]:
    return classification.to_dict() if classification else {}


def _pdf_info_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Shared PDF helpers
# ---------------------------------------------------------------------------


def extract_pdf_structural_metadata(file_data: BinaryIO) -> dict[str, Any]:
    """Extract cheap PDF facts with pypdf. Empty dict on hard failure."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"extractor_warning": "pypdf_not_installed"}

    try:
        _seek_start(file_data)
        reader = PdfReader(file_data)
        page_count = len(reader.pages)
        extra: dict[str, Any] = {
            "page_count": page_count,
            "encrypted": bool(getattr(reader, "is_encrypted", False)),
        }

        meta = getattr(reader, "metadata", None)
        if meta is not None:
            doc_meta = {
                "title": _pdf_info_str(getattr(meta, "title", None)),
                "author": _pdf_info_str(getattr(meta, "author", None)),
                "subject": _pdf_info_str(getattr(meta, "subject", None)),
                "creator": _pdf_info_str(getattr(meta, "creator", None)),
                "producer": _pdf_info_str(getattr(meta, "producer", None)),
            }
            # Drop empty keys to keep summary compact
            extra["document"] = {k: v for k, v in doc_meta.items() if v}

        # First-page text sample length — used by classifier and consumers
        first_page_chars = 0
        if page_count > 0:
            try:
                text = (reader.pages[0].extract_text() or "").strip()
                first_page_chars = len(text)
            except Exception:
                first_page_chars = 0
        extra["first_page_text_chars"] = first_page_chars
        return extra
    except Exception as exc:
        return {"extractor_warning": f"pdf_read_failed:{type(exc).__name__}"}
    finally:
        _seek_start(file_data)


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


class GenericMetadataExtractor(MetadataExtractor):
    """Fallback：MIME + size + classification。"""

    name = "metadata"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        extra = _classification_extra(classification)
        extra["filename"] = filename
        extra["extension"] = Path(filename).suffix.lower() or None
        return MetadataResult(
            mime_type=resolved_mime,
            size_bytes=_size_of(file_data),
            extra=extra,
        )


class PdfDigitalMetadataExtractor(GenericMetadataExtractor):
    """数字 PDF：有文本层，推荐 layout。"""

    name = "pdf-digital-metadata"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        extra = extract_pdf_structural_metadata(file_data)
        extra.update(base.extra)
        extra["text_layer"] = "present"
        extra["ocr_recommended"] = False
        extra["recommended_profiles"] = ["layout"]
        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)


class PdfScannedMetadataExtractor(GenericMetadataExtractor):
    """扫描 PDF：无（或极弱）文本层，推荐 ocr → layout。"""

    name = "pdf-scanned-metadata"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        extra = extract_pdf_structural_metadata(file_data)
        extra.update(base.extra)
        extra["text_layer"] = "absent"
        extra["ocr_recommended"] = True
        extra["recommended_profiles"] = ["ocr", "layout"]
        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)


class SpreadsheetMetadataExtractor(GenericMetadataExtractor):
    """表格：sheet 数、sheet 名、各 sheet 维度（尽力而为）。支持 xlsx / xls；WPS et/ett 仅浅字段。"""

    name = "spreadsheet-metadata"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        subtype = (classification.subtype if classification else "") or ""
        suffix = Path(filename).suffix.lower()
        if subtype in {"et", "ett"} or suffix in {".et", ".ett"}:
            # 原生 WPS 表格：L0 仅记格式；layout 前经 LibreOffice→xlsx
            fmt = "ett" if (subtype == "ett" or suffix == ".ett") else "et"
            sheet_meta = {
                "workbook_format": fmt,
                "extractor_warning": "wps_et_limited_metadata",
                "recommended_profiles": ["layout", "tables"],
            }
        elif subtype == "xls" or suffix == ".xls":
            sheet_meta = self._xls_metadata(file_data)
        else:
            sheet_meta = self._xlsx_metadata(file_data)

        extra = {**sheet_meta, **base.extra}
        extra["recommended_profiles"] = list(
            dict.fromkeys([*(extra.get("recommended_profiles") or []), "layout"])
        )
        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)

    def _xlsx_metadata(self, file_data: BinaryIO) -> dict[str, Any]:
        try:
            from openpyxl import load_workbook
        except ImportError:
            return {"extractor_warning": "openpyxl_not_installed"}

        try:
            _seek_start(file_data)
            workbook = load_workbook(file_data, read_only=True, data_only=False)
            try:
                names = list(workbook.sheetnames)
                sheets: list[dict[str, Any]] = []
                for name in names:
                    ws = workbook[name]
                    # read_only: dimensions may be None until accessed
                    dims = getattr(ws, "dimensions", None)
                    max_row = getattr(ws, "max_row", None)
                    max_col = getattr(ws, "max_column", None)
                    sheets.append(
                        {
                            "name": name,
                            "max_row": max_row,
                            "max_column": max_col,
                            "dimensions": dims,
                        }
                    )
                return {
                    "sheet_count": len(names),
                    "sheet_names": names,
                    "sheets": sheets,
                    "workbook_format": "xlsx",
                }
            finally:
                workbook.close()
        except Exception as exc:
            return {"extractor_warning": f"xlsx_read_failed:{type(exc).__name__}"}
        finally:
            _seek_start(file_data)

    def _xls_metadata(self, file_data: BinaryIO) -> dict[str, Any]:
        try:
            import xlrd
        except ImportError:
            return {"extractor_warning": "xlrd_not_installed"}

        try:
            _seek_start(file_data)
            payload = file_data.read()
            book = xlrd.open_workbook(file_contents=payload, on_demand=True)
            try:
                names = list(book.sheet_names())
                sheets: list[dict[str, Any]] = []
                for name in names:
                    sheet = book.sheet_by_name(name)
                    sheets.append(
                        {
                            "name": name,
                            "max_row": sheet.nrows,
                            "max_column": sheet.ncols,
                        }
                    )
                return {
                    "sheet_count": len(names),
                    "sheet_names": names,
                    "sheets": sheets,
                    "workbook_format": "xls",
                }
            finally:
                book.release_resources()
        except Exception as exc:
            return {"extractor_warning": f"xls_read_failed:{type(exc).__name__}"}
        finally:
            _seek_start(file_data)


class OfficeMetadataExtractor(GenericMetadataExtractor):
    """Office 文档：docx 段落/表格/核心属性；旧 .doc 仅记格式提示。"""

    name = "office-metadata"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        subtype = (classification.subtype if classification else "") or ""
        suffix = Path(filename).suffix.lower()

        if subtype == "docx" or suffix == ".docx":
            office_meta = self._docx_metadata(file_data)
        elif subtype == "doc" or suffix == ".doc":
            office_meta = {
                "office_format": "doc",
                "extractor_warning": "legacy_doc_limited_metadata",
                "recommended_profiles": ["layout"],
            }
        elif subtype in {"wps", "wpt"} or suffix in {".wps", ".wpt"}:
            fmt = "wpt" if (subtype == "wpt" or suffix == ".wpt") else "wps"
            office_meta = {
                "office_format": fmt,
                "extractor_warning": "wps_limited_metadata",
                "recommended_profiles": ["layout"],
            }
        elif subtype in {"dps", "dpt"} or suffix in {".dps", ".dpt"}:
            fmt = "dpt" if (subtype == "dpt" or suffix == ".dpt") else "dps"
            office_meta = {
                "office_format": fmt,
                "extractor_warning": "wps_dps_limited_metadata",
                "recommended_profiles": ["layout"],
            }
        else:
            office_meta = {"office_format": subtype or "unknown"}

        extra = {**office_meta, **base.extra}
        extra.setdefault("recommended_profiles", ["layout"])
        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)

    def _docx_metadata(self, file_data: BinaryIO) -> dict[str, Any]:
        try:
            from docx import Document
        except ImportError:
            return {"extractor_warning": "python_docx_not_installed", "office_format": "docx"}

        try:
            _seek_start(file_data)
            document = Document(file_data)
            paragraphs = list(document.paragraphs)
            non_empty = [p.text.strip() for p in paragraphs if (p.text or "").strip()]
            tables = list(document.tables)

            core = document.core_properties
            doc_meta = {
                "title": _pdf_info_str(core.title),
                "author": _pdf_info_str(core.author),
                "subject": _pdf_info_str(core.subject),
                "last_modified_by": _pdf_info_str(core.last_modified_by),
            }
            document_info = {k: v for k, v in doc_meta.items() if v}

            # Lightweight heading-style outline (style name starts with Heading)
            headings: list[str] = []
            for p in paragraphs:
                style_name = getattr(getattr(p, "style", None), "name", "") or ""
                if style_name.lower().startswith("heading") and (p.text or "").strip():
                    headings.append(p.text.strip()[:200])
                    if len(headings) >= 20:
                        break

            return {
                "office_format": "docx",
                "paragraph_count": len(paragraphs),
                "non_empty_paragraph_count": len(non_empty),
                "table_count": len(tables),
                "heading_count": len(headings),
                "headings_preview": headings[:10],
                "document": document_info,
                "recommended_profiles": ["layout"],
            }
        except Exception as exc:
            return {
                "office_format": "docx",
                "extractor_warning": f"docx_read_failed:{type(exc).__name__}",
                "recommended_profiles": ["layout"],
            }
        finally:
            _seek_start(file_data)


class TextMetadataExtractor(GenericMetadataExtractor):
    """纯文本 / Markdown / CSV：行数、字符数、编码探测、CSV 列数。"""

    name = "text-metadata"
    version = "0.2.0"

    _ENCODING_CANDIDATES = ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1")

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        _seek_start(file_data)
        raw = file_data.read() or b""
        _seek_start(file_data)

        text, encoding = self._decode(raw)
        lines = text.splitlines()
        subtype = (classification.subtype if classification else "") or "plain"

        extra: dict[str, Any] = {
            **base.extra,
            "line_count": len(lines),
            "char_count": len(text),
            "encoding": encoding,
            "is_empty": len(text.strip()) == 0,
            "recommended_profiles": ["layout"],
        }

        if subtype == "csv" or Path(filename).suffix.lower() == ".csv":
            extra.update(self._csv_hints(text))
        elif subtype == "markdown" or Path(filename).suffix.lower() == ".md":
            headings = re.findall(r"(?m)^(#{1,6})\s+(.+)$", text)
            extra["markdown_heading_count"] = len(headings)
            extra["headings_preview"] = [h[1].strip()[:200] for h in headings[:10]]

        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)

    def _decode(self, raw: bytes) -> tuple[str, str]:
        for enc in self._ENCODING_CANDIDATES:
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace"), "utf-8-replace"

    def _csv_hints(self, text: str) -> dict[str, Any]:
        sample = text[:8192]
        if not sample.strip():
            return {"csv_column_count": 0, "csv_dialect": None}
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
            dialect = None

        try:
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
        except csv.Error:
            return {"csv_column_count": 0, "csv_dialect": delimiter}

        col_count = max((len(r) for r in rows), default=0)
        return {
            "csv_column_count": col_count,
            "csv_row_count": len(rows),
            "csv_dialect": delimiter,
            "csv_has_header_guess": bool(rows) and all(cell and not cell.replace(".", "", 1).isdigit() for cell in rows[0]),
        }


class ImageMetadataExtractor(GenericMetadataExtractor):
    """图片：宽高、格式、色彩模式；按 subtype 推荐 profile。"""

    name = "image-metadata"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        extra = dict(base.extra)
        extra.update(self._image_info(file_data))
        subtype = classification.subtype if classification else "photo"
        if subtype == "document_scan":
            extra["recommended_profiles"] = ["ocr", "layout", "deep"]
            extra["ocr_recommended"] = True
        elif subtype == "screenshot":
            extra["recommended_profiles"] = ["layout", "deep"]
            extra["ocr_recommended"] = False
        else:
            extra["recommended_profiles"] = ["layout", "deep"]
            extra["ocr_recommended"] = False
        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)

    def _image_info(self, file_data: BinaryIO) -> dict[str, Any]:
        try:
            from PIL import Image
        except ImportError:
            return {"extractor_warning": "pillow_not_installed"}

        try:
            _seek_start(file_data)
            with Image.open(file_data) as image:
                info: dict[str, Any] = {
                    "width": image.width,
                    "height": image.height,
                    "image_format": image.format,
                    "image_mode": image.mode,
                }
                if getattr(image, "is_animated", False):
                    info["frame_count"] = getattr(image, "n_frames", 1)
                return info
        except Exception as exc:
            return {"extractor_warning": f"image_read_failed:{type(exc).__name__}"}
        finally:
            _seek_start(file_data)


class AudioMetadataExtractor(GenericMetadataExtractor):
    """Audio L0：duration / channels / sample_rate when cheaply available."""

    name = "metadata_audio"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        extra = dict(base.extra or {})
        extra.update(self._audio_info(file_data, filename, mime_type))
        extra.setdefault("recommended_profiles", ["layout"])
        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)

    def _audio_info(self, file_data: BinaryIO, filename: str, mime_type: str) -> dict[str, Any]:
        lower = (filename or "").lower()
        # WAV via stdlib
        if lower.endswith(".wav") or "wav" in (mime_type or "").lower():
            try:
                import wave

                _seek_start(file_data)
                with wave.open(file_data, "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate() or 1
                    channels = wf.getnchannels()
                    duration = frames / float(rate) if rate else None
                    return {
                        "duration_seconds": round(duration, 3) if duration is not None else None,
                        "sample_rate": rate,
                        "channels": channels,
                        "audio_format": "wav",
                        "sample_width_bytes": wf.getsampwidth(),
                    }
            except Exception as exc:
                return {"extractor_warning": f"wav_read_failed:{type(exc).__name__}"}
            finally:
                _seek_start(file_data)

        # Optional mutagen for other containers
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            return {"extractor_warning": "mutagen_not_installed"}

        try:
            _seek_start(file_data)
            data = file_data.read()
            _seek_start(file_data)
            audio = MutagenFile(io.BytesIO(data))
            if audio is None:
                return {"extractor_warning": "audio_unrecognized"}
            info: dict[str, Any] = {"audio_format": type(audio).__name__}
            length = getattr(getattr(audio, "info", None), "length", None)
            if length is not None:
                info["duration_seconds"] = round(float(length), 3)
            channels = getattr(getattr(audio, "info", None), "channels", None)
            if channels is not None:
                info["channels"] = int(channels)
            rate = getattr(getattr(audio, "info", None), "sample_rate", None) or getattr(
                getattr(audio, "info", None), "samplerate", None
            )
            if rate is not None:
                info["sample_rate"] = int(rate)
            return info
        except Exception as exc:
            return {"extractor_warning": f"audio_read_failed:{type(exc).__name__}"}
        finally:
            _seek_start(file_data)


class VideoMetadataExtractor(GenericMetadataExtractor):
    """Video L0：duration when mutagen available; otherwise size+mime only."""

    name = "metadata_video"
    version = "0.2.0"

    def extract(
        self,
        file_data: BinaryIO,
        filename: str,
        mime_type: str,
        *,
        classification: FileClassification | None = None,
    ) -> MetadataResult:
        base = super().extract(file_data, filename, mime_type, classification=classification)
        extra = dict(base.extra or {})
        extra.update(self._video_info(file_data, filename))
        extra.setdefault("recommended_profiles", ["layout"])
        return MetadataResult(mime_type=base.mime_type, size_bytes=base.size_bytes, extra=extra)

    def _video_info(self, file_data: BinaryIO, filename: str) -> dict[str, Any]:
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            return {"extractor_warning": "mutagen_not_installed"}

        try:
            _seek_start(file_data)
            data = file_data.read()
            _seek_start(file_data)
            media = MutagenFile(io.BytesIO(data))
            if media is None:
                return {"extractor_warning": "video_unrecognized"}
            info: dict[str, Any] = {"video_format": type(media).__name__}
            length = getattr(getattr(media, "info", None), "length", None)
            if length is not None:
                info["duration_seconds"] = round(float(length), 3)
            return info
        except Exception as exc:
            return {"extractor_warning": f"video_read_failed:{type(exc).__name__}"}
        finally:
            _seek_start(file_data)
