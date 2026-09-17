"""轻量 layout 解析器：按行切分纯文本。

仅用于单元测试或无 markitdown 依赖的场景。
生产 / 智能体联调请用 LAYOUT_PARSER_NAME=markitdown（文件 → Markdown 字符串）。
"""
from __future__ import annotations

from pathlib import Path

from app.parsers.base import ParseContext, Parser
from app.schemas.content import ContentBlock, ContentSource, DocumentInfo, StructuredContent


class SimpleLayoutParser(Parser):
    name = "simple"
    version = "0.2.0"
    profile = "layout"
    supported_mime_types = [
        "text/plain",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]

    def can_handle(self, mime_type: str) -> bool:
        return mime_type in self.supported_mime_types

    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        from app.parsers.structure_blocks import split_markdown_to_blocks

        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        # Prefer sheet / form-feed multi-block; else one block per non-empty line (test-friendly)
        structured_blocks, page_count = split_markdown_to_blocks(
            text,
            mime_type=context.mime_type,
            filename=context.filename,
        )
        multi_structural = len(structured_blocks) >= 2 and any(
            (b.metadata or {}).get("sheet_name") or b.id.startswith("page-")
            for b in structured_blocks
        )
        if multi_structural:
            blocks = structured_blocks
        else:
            lines = [line for line in text.splitlines() if line.strip()]
            blocks = [
                ContentBlock(
                    id=f"b-{index:03d}",
                    type="heading" if line.startswith("#") else "paragraph",
                    page=1,
                    text=line.lstrip("# ").strip(),
                    confidence=1.0,
                )
                for index, line in enumerate(lines, start=1)
            ] or structured_blocks
            page_count = 1
        return StructuredContent(
            content_hash=context.content_hash,
            source=ContentSource(
                parser_name=self.name,
                parser_version=self.version,
                profile=self.profile,
            ),
            document=DocumentInfo(page_count=page_count),
            blocks=blocks,
            metadata={
                "format": "text",
                "markdown": text,
                "char_count": len(text),
                "block_count": len(blocks),
                "filename": context.filename,
                "mime_type": context.mime_type,
            },
        )