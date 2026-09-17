"""读路径 content 视图：大纲、sheet 过滤、字符窗口（不改写 parse 缓存）。"""
from __future__ import annotations

import re
from typing import Any


_SHEET_SPLIT = re.compile(r"(?m)(?=^## )")
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def parse_sheets_param(raw: str | None) -> set[str] | None:
    """Parse comma-separated sheet names; None means no filter."""
    if raw is None or not str(raw).strip():
        return None
    names: set[str] = set()
    for part in str(raw).split(","):
        name = part.strip()
        if name.startswith("## "):
            name = name[3:].strip()
        if name:
            names.add(name.casefold())
    return names or None


def filter_markdown_by_sheets(markdown: str, sheet_names: set[str] | None) -> str:
    """Keep only ``## Sheet`` sections whose titles match *sheet_names* (casefold)."""
    if not sheet_names or not markdown:
        return markdown
    parts = _SHEET_SPLIT.split(markdown)
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if not part.startswith("## "):
            # Preface before first sheet — drop when filtering sheets
            continue
        title_line = part.splitlines()[0]
        title = title_line[3:].strip()
        if title.casefold() in sheet_names:
            kept.append(part.rstrip())
    if not kept:
        return ""
    return "\n\n".join(kept) + "\n"


def extract_outline_from_markdown(markdown: str) -> list[dict[str, Any]]:
    """Extract heading / sheet outline entries from markdown text."""
    if not markdown:
        return []
    items: list[dict[str, Any]] = []
    for match in _HEADING_LINE.finditer(markdown):
        level = len(match.group(1))
        title = match.group(2).strip()
        items.append({"level": level, "title": title, "offset": match.start()})
    return items


def extract_outline_from_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """Outline from structured blocks (heading / sheet metadata)."""
    items: list[dict[str, Any]] = []
    for block in blocks:
        meta = getattr(block, "metadata", None) or {}
        sheet = meta.get("sheet_name") if isinstance(meta, dict) else None
        text = getattr(block, "text", None) or ""
        btype = getattr(block, "type", None)
        if sheet:
            items.append(
                {
                    "level": 2,
                    "title": str(sheet),
                    "block_id": getattr(block, "id", None),
                    "page": getattr(block, "page", None),
                    "kind": "sheet",
                }
            )
        elif btype == "heading" and text:
            first = text.splitlines()[0].strip().lstrip("# ").strip()
            items.append(
                {
                    "level": 1,
                    "title": first,
                    "block_id": getattr(block, "id", None),
                    "page": getattr(block, "page", None),
                    "kind": "heading",
                }
            )
        elif text and text.lstrip().startswith("#"):
            m = _HEADING_LINE.match(text.splitlines()[0])
            if m:
                items.append(
                    {
                        "level": len(m.group(1)),
                        "title": m.group(2).strip(),
                        "block_id": getattr(block, "id", None),
                        "page": getattr(block, "page", None),
                        "kind": "heading",
                    }
                )
    return items


def apply_char_window(
    markdown: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Slice markdown by character offset/max_chars. Returns (text, meta flags)."""
    offset = max(0, int(offset or 0))
    text = markdown or ""
    total = len(text)
    if offset > total:
        offset = total
    end = total
    if max_chars is not None and max_chars >= 0:
        end = min(total, offset + max_chars)
    window = text[offset:end]
    truncated = end < total
    next_offset: int | None = end if truncated else None
    meta: dict[str, Any] = {
        "truncated": truncated,
        "char_count": len(window),
        "total_char_count": total,
        "offset": offset,
        "next_offset": next_offset,
    }
    if max_chars is not None:
        meta["max_chars"] = max_chars
    return window, meta


def build_outline_markdown(outline: list[dict[str, Any]]) -> str:
    """Render outline items as a compact markdown list."""
    if not outline:
        return ""
    lines: list[str] = []
    for item in outline:
        level = int(item.get("level") or 1)
        title = str(item.get("title") or "")
        indent = "  " * max(0, level - 1)
        lines.append(f"{indent}- {title}")
    return "\n".join(lines) + "\n"


def apply_content_view(
    content: Any,
    *,
    mode: str = "full",
    max_chars: int | None = None,
    offset: int = 0,
    sheets: str | None = None,
    mime_type: str | None = None,
    filename: str | None = None,
) -> Any:
    """Apply agent-friendly view transforms to StructuredContent (read path only)."""
    mode_norm = (mode or "full").strip().lower()
    if mode_norm not in {"full", "outline"}:
        mode_norm = "full"

    metadata = dict(content.metadata or {})
    mime = mime_type or metadata.get("mime_type")
    fname = filename or metadata.get("filename")

    # Resolve primary markdown
    raw_md = metadata.get("markdown")
    if not isinstance(raw_md, str):
        if content.blocks:
            raw_md = "\n\n".join(
                (b.text or "") for b in content.blocks if getattr(b, "text", None)
            )
        else:
            raw_md = ""

    sheet_set = parse_sheets_param(sheets)
    if sheet_set is not None:
        # Prefer markdown sheet filter; also filter blocks with sheet_name
        raw_md = filter_markdown_by_sheets(raw_md, sheet_set)
        blocks = []
        for block in content.blocks:
            meta = getattr(block, "metadata", None) or {}
            sheet_name = meta.get("sheet_name") if isinstance(meta, dict) else None
            if sheet_name is not None:
                if str(sheet_name).casefold() in sheet_set:
                    blocks.append(block)
            else:
                # monoblock: replace text with filtered markdown later
                blocks.append(block)
        # tables profile: filter ContentTable + tables_json
        tables = []
        for table in content.tables or []:
            tmeta = getattr(table, "metadata", None) or {}
            tname = tmeta.get("sheet_name") if isinstance(tmeta, dict) else None
            if tname is None or str(tname).casefold() in sheet_set:
                tables.append(table)
        tables_json = metadata.get("tables_json")
        if isinstance(tables_json, dict) and isinstance(tables_json.get("sheets"), list):
            filtered_sheets = [
                s
                for s in tables_json["sheets"]
                if str(s.get("name") or "").casefold() in sheet_set
            ]
            tables_json = {
                **tables_json,
                "sheet_count": len(filtered_sheets),
                "sheets": filtered_sheets,
            }
            metadata["tables_json"] = tables_json
            metadata["sheet_names"] = [s.get("name") for s in filtered_sheets]
            metadata["sheet_count"] = len(filtered_sheets)
        content = content.model_copy(update={"blocks": blocks, "tables": tables})
        metadata["sheets_filter"] = sorted(sheet_set)

    if mode_norm == "outline":
        outline = extract_outline_from_blocks(content.blocks)
        if not outline:
            outline = extract_outline_from_markdown(raw_md)
        outline_md = build_outline_markdown(outline)
        metadata = {
            **metadata,
            "mode": "outline",
            "outline": outline,
            "markdown": outline_md,
            "char_count": len(outline_md),
            "truncated": False,
            "offset": 0,
            "next_offset": None,
            "total_char_count": len(outline_md),
        }
        from app.schemas.content import ContentBlock

        outline_block = ContentBlock(
            id="outline-001",
            type="list",
            page=1,
            text=outline_md,
            confidence=1.0,
            metadata={"format": "outline"},
        )
        return content.model_copy(
            update={
                "blocks": [outline_block],
                "tables": [],
                "metadata": metadata,
            }
        )

    # full mode: optional char window on markdown
    window_applied = bool(offset) or max_chars is not None
    window_md, window_meta = apply_char_window(
        raw_md,
        offset=offset,
        max_chars=max_chars,
    )
    metadata = {
        **metadata,
        "mode": "full",
        **window_meta,
    }
    # Always expose markdown string for agents; windowed when requested
    metadata["markdown"] = window_md

    blocks = list(content.blocks)
    if window_applied:
        # Character window is a read-path view: collapse to a single view block
        from app.schemas.content import ContentBlock

        blocks = [
            ContentBlock(
                id="view-001",
                type="paragraph",
                page=1,
                text=window_md,
                confidence=1.0,
                metadata={"format": "markdown", "windowed": True},
            )
        ]
    elif sheet_set is not None and len(blocks) == 1 and blocks[0].text is not None:
        # Monoblock spreadsheet after sheet filter: align block text with filtered markdown
        blocks[0] = blocks[0].model_copy(update={"text": window_md})

    if mime:
        metadata.setdefault("mime_type", mime)
    if fname:
        metadata.setdefault("filename", fname)

    return content.model_copy(update={"blocks": blocks, "metadata": metadata})
