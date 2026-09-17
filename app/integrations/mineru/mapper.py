"""将 MinerU content_list.json 映射为统一 StructuredContent。"""
from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Any

from app.integrations.mineru.zip_utils import find_content_list_json
from app.schemas.content import (
    ContentBlock,
    ContentSource,
    ContentTable,
    ContentWarning,
    DocumentInfo,
    StructuredContent,
)

TABLE_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    return unescape(TAG_RE.sub("", value)).strip()


def _parse_table_rows(table_html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    row_chunks = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.IGNORECASE | re.DOTALL)
    for row_html in row_chunks:
        cells = [_strip_html(cell) for cell in TABLE_CELL_RE.findall(row_html)]
        if cells:
            rows.append(cells)
    return rows


def _block_type_for_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("#"):
        return "heading"
    return "paragraph"


def _confidence_from_item(item: dict[str, Any]) -> float | None:
    for key in ("score", "confidence"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def map_content_list_to_structured(
    *,
    content_hash: str,
    parsed_dir: Path,
    parser_name: str,
    parser_version: str,
    profile: str,
) -> StructuredContent:
    content_list_path = find_content_list_json(parsed_dir)
    items: list[dict[str, Any]] = []
    if content_list_path and content_list_path.exists():
        raw = json.loads(content_list_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            items = [item for item in raw if isinstance(item, dict)]

    blocks: list[ContentBlock] = []
    tables: list[ContentTable] = []
    warnings: list[ContentWarning] = []
    pages: set[int] = set()

    for index, item in enumerate(items, start=1):
        item_type = item.get("type")
        page_idx = item.get("page_idx")
        page = page_idx + 1 if isinstance(page_idx, int) else 1
        pages.add(page)
        confidence = _confidence_from_item(item)
        bbox = item.get("bbox") if isinstance(item.get("bbox"), list) else None

        if item_type == "text":
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            blocks.append(
                ContentBlock(
                    id=f"b-{index:03d}",
                    type=_block_type_for_text(text),
                    page=page,
                    bbox=bbox,
                    text=text,
                    confidence=confidence,
                    metadata={"mineru_type": item_type},
                )
            )
        elif item_type == "table":
            table_body = str(item.get("table_body", "")).strip()
            if not table_body:
                continue
            rows = _parse_table_rows(table_body)
            table = ContentTable(
                id=f"t-{index:03d}",
                page=page,
                bbox=bbox,
                rows=rows,
                confidence=confidence,
                metadata={"html": table_body, "mineru_type": item_type},
            )
            if not rows:
                warnings.append(
                    ContentWarning(
                        block_id=table.id,
                        type="parse_partial",
                        detail="MinerU table HTML could not be converted to rows",
                        candidates=[table_body[:500]],
                    )
                )
            tables.append(table)
        elif item_type == "image":
            image_path = item.get("img_path") or item.get("image_path")
            blocks.append(
                ContentBlock(
                    id=f"b-{index:03d}",
                    type="image",
                    page=page,
                    bbox=bbox,
                    text=str(image_path or ""),
                    confidence=confidence,
                    metadata={"mineru_type": item_type},
                )
            )
        else:
            text = str(item.get("text") or item.get("table_body") or "").strip()
            if text:
                blocks.append(
                    ContentBlock(
                        id=f"b-{index:03d}",
                        type="paragraph",
                        page=page,
                        bbox=bbox,
                        text=text,
                        confidence=confidence,
                        metadata={"mineru_type": item_type or "unknown"},
                    )
                )

    full_md_path = parsed_dir / "full.md"
    metadata: dict[str, Any] = {"format": "markdown"}
    full_md = ""
    if full_md_path.exists():
        full_md = full_md_path.read_text(encoding="utf-8")
        metadata["full_md_lines"] = len(full_md.splitlines())
        metadata["markdown"] = full_md
        metadata["char_count"] = len(full_md)
    elif blocks:
        # 无 full.md 时用 blocks 文本拼接，保证 content 读路径有正文
        full_md = "\n\n".join((b.text or "") for b in blocks if b.text)
        metadata["markdown"] = full_md
        metadata["char_count"] = len(full_md)
        metadata["markdown_source"] = "blocks_fallback"

    if not items:
        warnings.append(
            ContentWarning(
                type="parse_partial",
                detail="MinerU result missing content_list.json; structured blocks unavailable",
            )
        )

    return StructuredContent(
        content_hash=content_hash,
        source=ContentSource(parser_name=parser_name, parser_version=parser_version, profile=profile),
        document=DocumentInfo(page_count=max(pages) if pages else None),
        blocks=blocks,
        tables=tables,
        warnings=warnings,
        metadata=metadata,
    )