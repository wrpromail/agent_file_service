"""MarkItDown layout 解析器：将文件转为 Markdown 字符串，供智能体直接消费。"""
from __future__ import annotations

from app.parsers.base import ParseContext, Parser
from app.schemas.content import ContentSource, ContentWarning, DocumentInfo, StructuredContent

# 常见可转 Markdown 的 MIME；未知类型仍会尝试 convert，失败则报错。
_DEFAULT_MIME_TYPES = [
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/pdf",
    "application/json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/bmp",
]


class MarkItDownLayoutParser(Parser):
    """把任意支持的文件转为 Markdown，并尽量拆成多 ContentBlock。

    智能体侧推荐：
        markdown = content.metadata["markdown"]  # 完整原文
    或按 block / content 读路径裁剪（sheets / max_chars / mode=outline）。
    """

    name = "markitdown"
    version = "0.2.0"
    profile = "layout"
    supported_mime_types = list(_DEFAULT_MIME_TYPES)

    def can_handle(self, mime_type: str) -> bool:
        if not mime_type:
            return True
        if mime_type in self.supported_mime_types:
            return True
        # 文本族 / 图片族宽放行，具体能否转换由 markitdown 决定
        return mime_type.startswith("text/") or mime_type.startswith("image/")

    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:  # pragma: no cover - 依赖在 parsers extra 中
            raise RuntimeError(
                "markitdown is not installed; install with: pip install 'file-service[parsers]'"
            ) from exc

        converter = MarkItDown()
        result = converter.convert(file_path)
        markdown = _extract_markdown(result)

        from app.parsers.structure_blocks import split_markdown_to_blocks

        blocks, page_count = split_markdown_to_blocks(
            markdown,
            mime_type=context.mime_type,
            filename=context.filename,
        )

        warnings: list[ContentWarning] = []
        if not markdown.strip():
            warnings.append(
                ContentWarning(
                    block_id=blocks[0].id if blocks else "md-001",
                    type="parse_partial",
                    detail="MarkItDown returned empty markdown content",
                )
            )

        return StructuredContent(
            content_hash=context.content_hash,
            source=ContentSource(
                parser_name=self.name,
                parser_version=self.version,
                profile=self.profile,
            ),
            document=DocumentInfo(page_count=page_count),
            blocks=blocks,
            tables=[],
            warnings=warnings,
            metadata={
                "format": "markdown",
                "markdown": markdown,
                "char_count": len(markdown),
                "block_count": len(blocks),
                "title": getattr(result, "title", None),
                "filename": context.filename,
                "mime_type": context.mime_type,
            },
        )


def _extract_markdown(result: object) -> str:
    """兼容不同 markitdown 版本的 result 字段名。"""
    for attr in ("text_content", "markdown"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""
