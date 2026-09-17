"""将 Markdown 拆成多 ContentBlock（结构切分最低可用）。

完整原文仍应写入 metadata.markdown；本模块只生成 blocks / page_count。
"""
from __future__ import annotations

import re

from app.schemas.content import ContentBlock

_SHEET_SPLIT = re.compile(r"(?m)(?=^## )")
_PAGE_FORM_FEED = re.compile(r"\f")
_PAGE_HTML_COMMENT = re.compile(r"(?i)<!--\s*page\s+(\d+)\s*-->")
_H1_H2_SPLIT = re.compile(r"(?m)(?=^#{1,2} )")


def _slug(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "-", text.strip(), flags=re.UNICODE)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-").lower()
    return cleaned[:64] or fallback


def split_spreadsheet_sheets(markdown: str) -> list[ContentBlock] | None:
    """Split Excel/CSV style ``## Sheet`` sections. None if not sheet-like."""
    if not markdown or not re.search(r"(?m)^## .+$", markdown):
        return None
    parts = _SHEET_SPLIT.split(markdown)
    blocks: list[ContentBlock] = []
    page = 0
    for part in parts:
        if not part.strip() or not part.startswith("## "):
            continue
        page += 1
        lines = part.splitlines()
        title = lines[0][3:].strip()
        body = "\n".join(lines[1:]).strip()
        text = part.rstrip() + ("\n" if not part.endswith("\n") else "")
        blocks.append(
            ContentBlock(
                id=f"sheet-{_slug(title, f'{page:03d}')}",
                type="table" if "|" in body else "paragraph",
                page=page,
                text=text,
                confidence=1.0,
                metadata={"sheet_name": title, "format": "markdown"},
            )
        )
    return blocks or None


def split_by_form_feed(markdown: str) -> list[ContentBlock] | None:
    if "\f" not in markdown:
        return None
    pages = _PAGE_FORM_FEED.split(markdown)
    blocks: list[ContentBlock] = []
    for index, page_text in enumerate(pages, start=1):
        text = page_text.strip()
        if not text:
            continue
        blocks.append(
            ContentBlock(
                id=f"page-{index:03d}",
                type="paragraph",
                page=index,
                text=text,
                confidence=1.0,
                metadata={"format": "markdown"},
            )
        )
    return blocks or None


def split_by_page_comments(markdown: str) -> list[ContentBlock] | None:
    if not _PAGE_HTML_COMMENT.search(markdown):
        return None
    parts = _PAGE_HTML_COMMENT.split(markdown)
    # split with capture: [pre, page_num, body, page_num, body, ...]
    blocks: list[ContentBlock] = []
    if parts[0].strip():
        blocks.append(
            ContentBlock(
                id="page-000",
                type="paragraph",
                page=1,
                text=parts[0].strip(),
                confidence=1.0,
                metadata={"format": "markdown"},
            )
        )
    i = 1
    while i + 1 < len(parts):
        try:
            page_no = int(parts[i])
        except ValueError:
            page_no = len(blocks) + 1
        body = parts[i + 1].strip()
        if body:
            blocks.append(
                ContentBlock(
                    id=f"page-{page_no:03d}",
                    type="paragraph",
                    page=page_no,
                    text=body,
                    confidence=1.0,
                    metadata={"format": "markdown"},
                )
            )
        i += 2
    return blocks or None


def split_by_headings(markdown: str) -> list[ContentBlock] | None:
    """Split on H1/H2. Returns None if fewer than 2 sections."""
    if not markdown or not re.search(r"(?m)^#{1,2} ", markdown):
        return None
    parts = _H1_H2_SPLIT.split(markdown)
    sections = [p for p in parts if p.strip()]
    if len(sections) < 2:
        return None
    blocks: list[ContentBlock] = []
    for index, part in enumerate(sections, start=1):
        first = part.splitlines()[0] if part.splitlines() else ""
        m = re.match(r"^(#{1,2})\s+(.+)$", first)
        title = m.group(2).strip() if m else f"section-{index}"
        btype = "heading" if m else "paragraph"
        blocks.append(
            ContentBlock(
                id=f"sec-{index:03d}-{_slug(title, f'{index:03d}')}",
                type=btype,
                page=index,
                text=part.rstrip() + "\n",
                confidence=1.0,
                metadata={"format": "markdown", "section_title": title},
            )
        )
    return blocks


def split_markdown_to_blocks(
    markdown: str,
    *,
    mime_type: str | None = None,
    filename: str | None = None,
) -> tuple[list[ContentBlock], int]:
    """Return (blocks, page_count) for layout parsers.

    Always returns at least one block when markdown is non-empty.
    """
    from app.parsers.smart_markdown import is_spreadsheet

    text = markdown or ""
    if is_spreadsheet(mime_type=mime_type, filename=filename):
        sheets = split_spreadsheet_sheets(text)
        if sheets:
            return sheets, len(sheets)

    for splitter in (split_by_form_feed, split_by_page_comments, split_by_headings):
        blocks = splitter(text)
        if blocks and len(blocks) >= 2:
            pages = max(b.page for b in blocks)
            return blocks, pages

    # Fallback monoblock
    if not text.strip():
        return [
            ContentBlock(
                id="md-001",
                type="paragraph",
                page=1,
                text=text,
                confidence=1.0,
                metadata={"format": "markdown"},
            )
        ], 1

    # Prefer sheet split even without mime hint when ## present with table-like body
    sheets = split_spreadsheet_sheets(text)
    if sheets and len(sheets) >= 2:
        return sheets, len(sheets)

    return [
        ContentBlock(
            id="md-001",
            type="paragraph",
            page=1,
            text=text if text.endswith("\n") or not text else text + "\n",
            confidence=1.0,
            metadata={"format": "markdown"},
        )
    ], 1
