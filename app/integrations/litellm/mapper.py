from __future__ import annotations

from app.integrations.litellm.client import (
    VisionAnalysisResult,
    markdown_from_legacy_vision_fields,
)
from app.parsers.base import ParseContext
from app.schemas.content import ContentBlock, ContentSource, DocumentInfo, StructuredContent


def map_vision_result_to_structured(
    result: VisionAnalysisResult,
    *,
    context: ParseContext,
    parser_name: str,
    parser_version: str,
    profile: str = "deep",
) -> StructuredContent:
    """将视觉识别结果映射为 StructuredContent。

    主交付物为 ``metadata.markdown``（模型直接产出的结构 Markdown，或旧 JSON 合成结果）。
    """
    markdown = (result.markdown or "").strip()
    if not markdown:
        markdown = markdown_from_legacy_vision_fields(
            description=result.description or "",
            scene=result.scene or "",
            visible_text=list(result.visible_text or []),
        )

    blocks: list[ContentBlock] = []
    if markdown:
        blocks.append(
            ContentBlock(
                id="b-001",
                type="paragraph",
                page=1,
                text=markdown,
                confidence=result.confidence,
                metadata={"kind": "markdown", "media_kind": "image"},
            )
        )
    # 兼容：若有分条 visible_text 且与 markdown 不同，保留为附加块供调试
    elif result.visible_text:
        for index, text in enumerate(result.visible_text, start=1):
            blocks.append(
                ContentBlock(
                    id=f"b-text-{index:03d}",
                    type="paragraph",
                    page=1,
                    text=text,
                    confidence=result.confidence,
                    metadata={"kind": "visible_text"},
                )
            )

    return StructuredContent(
        content_hash=context.content_hash,
        source=ContentSource(
            parser_name=parser_name,
            parser_version=parser_version,
            profile=profile,
        ),
        document=DocumentInfo(page_count=1),
        blocks=blocks,
        metadata={
            "markdown": markdown,
            "format": "markdown",
            "char_count": len(markdown),
            "objects": result.objects,
            "scene": result.scene,
            "model": parser_name,
            "filename": context.filename,
            "mime_type": context.mime_type,
            "media_kind": "image",
        },
    )
