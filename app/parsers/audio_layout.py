"""音频 layout 解析器：经 LiteLLM 调用支持 audio 的模型做转写/简述。

供调用方的 profile=layout 路径消费（与图片视觉 layout 对齐）。
自定义指令仍走 POST .../custom-parse。
"""
from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.integrations.litellm.client import DEFAULT_AUDIO_USER_PROMPT, get_litellm_client
from app.parsers.base import ParseContext, Parser
from app.schemas.content import ContentBlock, ContentSource, DocumentInfo, StructuredContent


class AudioLayoutParser(Parser):
    profile = "layout"
    supported_mime_types = [
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
    ]

    def __init__(self, client=None, settings=None, *, profile: str | None = None) -> None:
        settings = settings or get_settings()
        self.name = settings.audio_parser_name
        self.version = settings.audio_parser_version
        self._client = client
        if profile:
            self.profile = profile

    def can_handle(self, mime_type: str) -> bool:
        return (mime_type or "").startswith("audio/")

    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        settings = get_settings()
        client = self._client or get_litellm_client()
        audio_bytes = Path(file_path).read_bytes()
        prompt = (settings.audio_default_prompt or "").strip() or DEFAULT_AUDIO_USER_PROMPT
        result = client.analyze_audio_with_prompt(
            audio_bytes,
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
                    metadata={"kind": "transcript"},
                )
            )
        return StructuredContent(
            content_hash=context.content_hash,
            source=ContentSource(
                parser_name=settings.audio_parser_name,
                parser_version=settings.audio_parser_version,
                profile=self.profile,
            ),
            document=DocumentInfo(page_count=1),
            blocks=blocks,
            metadata={
                "markdown": text,
                "format": "markdown",
                "char_count": len(text),
                "model": result.model or settings.audio_model,
                "filename": context.filename,
                "mime_type": context.mime_type,
                "media_kind": "audio",
            },
        )
