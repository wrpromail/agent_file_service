"""视频 layout 解析器：经 LiteLLM 调用支持 video 的模型做理解/转写。

供调用方的 profile=layout 路径消费（与图片/音频 layout 对齐）。
自定义指令仍走 POST .../custom-parse。
"""
from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.integrations.litellm.client import DEFAULT_VIDEO_USER_PROMPT, get_litellm_client
from app.parsers.base import ParseContext, Parser
from app.schemas.content import ContentBlock, ContentSource, DocumentInfo, StructuredContent


class VideoLayoutParser(Parser):
    profile = "layout"
    supported_mime_types = [
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-msvideo",
        "video/x-matroska",
        "video/mpeg",
        "video/3gpp",
        "video/x-m4v",
    ]

    def __init__(self, client=None, settings=None, *, profile: str | None = None) -> None:
        settings = settings or get_settings()
        self.name = settings.video_parser_name
        self.version = settings.video_parser_version
        self._client = client
        if profile:
            self.profile = profile

    def can_handle(self, mime_type: str) -> bool:
        return (mime_type or "").startswith("video/")

    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        settings = get_settings()
        client = self._client or get_litellm_client()
        video_bytes = Path(file_path).read_bytes()
        prompt = (settings.video_default_prompt or "").strip() or DEFAULT_VIDEO_USER_PROMPT
        result = client.analyze_video_with_prompt(
            video_bytes,
            mime_type=context.mime_type,
            prompt=prompt,
            filename=context.filename,
        )
        text = (result.text or "").strip()
        blocks: list[ContentBlock] = []
        if text:
            blocks.append(
                ContentBlock(
                    id="b-001",
                    type="paragraph",
                    page=1,
                    text=text,
                    confidence=None,
                    metadata={"kind": "video_summary"},
                )
            )
        return StructuredContent(
            content_hash=context.content_hash,
            source=ContentSource(
                parser_name=settings.video_parser_name,
                parser_version=settings.video_parser_version,
                profile=self.profile,
            ),
            document=DocumentInfo(page_count=1),
            blocks=blocks,
            metadata={
                "markdown": text,
                "format": "markdown",
                "char_count": len(text),
                "model": result.model or settings.video_model,
                "filename": context.filename,
                "mime_type": context.mime_type,
                "media_kind": "video",
            },
        )
