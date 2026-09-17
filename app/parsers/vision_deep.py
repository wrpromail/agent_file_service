"""图片视觉识别解析器：经 LiteLLM 调用视觉模型，输出结构化 Markdown。

可注册为 layout（Agent 默认读 layout）或 deep（显式 POST /parse profile=deep）。
主交付物与 Office/PDF 一致：``metadata.markdown``。
"""
from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.integrations.litellm.client import get_litellm_client
from app.integrations.litellm.mapper import map_vision_result_to_structured
from app.parsers.base import ParseContext, Parser
from app.schemas.content import StructuredContent


class VisionDeepParser(Parser):
    """默认 profile=deep；工厂可为 layout 注册独立实例。"""

    profile = "deep"
    supported_mime_types = [
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
        "image/gif",
        "image/bmp",
    ]

    def __init__(self, client=None, settings=None, *, profile: str | None = None) -> None:
        settings = settings or get_settings()
        self.name = settings.vision_parser_name
        self.version = settings.vision_parser_version
        self._client = client
        if profile:
            self.profile = profile

    def can_handle(self, mime_type: str) -> bool:
        return mime_type.startswith("image/")

    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        settings = get_settings()
        client = self._client or get_litellm_client()
        image_bytes = Path(file_path).read_bytes()
        result = client.analyze_image(
            image_bytes,
            mime_type=context.mime_type,
            filename=context.filename,
        )
        return map_vision_result_to_structured(
            result,
            context=context,
            parser_name=settings.vision_parser_name,
            parser_version=settings.vision_parser_version,
            profile=self.profile,
        )
