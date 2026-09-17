"""LiteLLM 代理客户端（OpenAI 兼容 /v1/chat/completions）。

出站 HTTP / 重试见 ``app.integrations.http``；本模块只负责多模态 payload 与结果映射。
"""
from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings, get_settings
from app.integrations.http import retry_policy_from_settings
from app.integrations.http.httpx_json import request_json as httpx_request_json

# 多模态默认：引导模型输出 Markdown，尽量还原媒体中的文本结构（供 Agent 读 metadata.markdown）
VISION_SYSTEM_PROMPT = (
    "你是图像文档还原助手。将图像中的可见文本与必要的客观画面信息还原为 Markdown 文档。\n"
    "要求：\n"
    "1. 优先还原文字结构：标题用 # / ##，列表用 - 或 1.，表格用 Markdown 表格，"
    "段落保持换行与层级；多栏/分区按阅读顺序分节。\n"
    "2. 发票、表单、证件、截图、白板等：按版面分区（标题、表头字段、明细表、备注、页脚等）。\n"
    "3. 代码、终端、UI 文案可用 fenced code block；公式可用行内/块级数学语法。\n"
    "4. 无文字时仅用简短客观描述；不要编造图像中不存在的文字或数字。\n"
    "5. 不做业务推断、不输出 JSON；直接输出 Markdown 正文，"
    "不要用 ```markdown 包裹全文，不要前言后语。"
)

CUSTOM_IMAGE_SYSTEM_PROMPT = (
    "你是图像分析助手。请严格依据用户给出的要求分析图像内容。\n"
    "优先用 Markdown 呈现结果，并尽量保留图像中文本的结构"
    "（标题、列表、表格、分区）。不要编造图像中不存在的信息。"
    "除非用户明确要求 JSON，否则不要输出 JSON。"
)

CUSTOM_AUDIO_SYSTEM_PROMPT = (
    "你是音频内容分析助手。请严格依据用户给出的要求处理音频"
    "（转写、摘要、要点提取等）。\n"
    "优先输出 Markdown：可用 ## 概要 / ## 转写 等分节；"
    "转写按说话人或时间线分段（若可辨识），列表/要点用 Markdown 列表。\n"
    "不要编造音频中不存在的内容。除非用户明确要求 JSON，否则不要输出 JSON。"
)

CUSTOM_VIDEO_SYSTEM_PROMPT = (
    "你是视频内容分析助手。请严格依据用户给出的要求处理视频"
    "（画面描述、情节概述、语音/字幕转写与要点提取等）。\n"
    "优先输出 Markdown：可用 ## 概述 / ## 画面 / ## 语音与字幕 / ## 时间线 等分节；"
    "时间码可用 `[mm:ss]`；列表与表格用 Markdown 语法。\n"
    "不要编造视频中不存在的内容。除非用户明确要求 JSON，否则不要输出 JSON。"
)

DEFAULT_VISION_USER_PROMPT = (
    "请将图片中的可见文本还原为结构清晰的 Markdown 文档"
    "（保留标题、列表、表格与阅读顺序）；必要时用一两句话客观描述画面。"
    "无文字则仅描述画面。"
)

DEFAULT_AUDIO_USER_PROMPT = (
    "请将音频内容整理为 Markdown 文档：\n"
    "## 概要\n（一两句主题；无明显语音可省略本节）\n"
    "## 转写\n完整转写语音；可按说话人或自然段落分段。"
    "若无明显语音请明确说明。"
)

DEFAULT_VIDEO_USER_PROMPT = (
    "请将视频内容整理为 Markdown 文档：\n"
    "## 概述\n画面与情节简要说明\n"
    "## 画面要点\n关键视觉信息（列表）\n"
    "## 语音与字幕\n转写或概括语音/字幕中的文本；可按时间分段\n"
    "若无明显有效内容请明确说明。"
)


@dataclass
class VisionAnalysisResult:
    """图片识别结果。主交付物为 markdown（与 Office/PDF layout 对齐）。"""

    markdown: str = ""
    description: str = ""
    objects: list[str] = field(default_factory=list)
    scene: str = ""
    visible_text: list[str] = field(default_factory=list)
    confidence: float | None = None
    raw_response: str = ""


@dataclass
class CustomMediaAnalysisResult:
    """用户自定义 prompt 的图片/音频/视频分析结果。"""

    text: str
    raw_response: str = ""
    model: str = ""


class LiteLLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        # 可注入 sleep 便于单测；默认 time.sleep
        self._sleep = sleep
        # 显式注入的 client 仅用于单测；生产路径用线程本地 client 以支持并发
        self._injected_http_client = http_client
        self._thread_local = threading.local()

    def _get_http_client(self):
        if self._injected_http_client is not None:
            return self._injected_http_client
        client = getattr(self._thread_local, "http_client", None)
        if client is not None:
            return client
        from app.integrations.http import httpx_json

        # Create via httpx_json.httpx so tests can patch that module.
        # 每线程独立 Client：httpx 非线程安全，且 vision-pages 多页并发需要真并行 I/O
        client = httpx_json.httpx.Client(timeout=60.0)
        self._thread_local.http_client = client
        return client

    @property
    def base_url(self) -> str:
        return self._settings.litellm_base_url.rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        api_key = self._settings.litellm_api_key
        if not api_key:
            raise RuntimeError("LITELLM_API_KEY is not configured")
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _retry_policy(self):
        return retry_policy_from_settings(self._settings, service="litellm")

    def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout: float,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """调用 ``/v1/chat/completions``（含可配置重试）。"""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        return httpx_request_json(
            "POST",
            f"{self.base_url}/v1/chat/completions",
            headers=self._auth_headers(),
            json=payload,
            timeout=timeout,
            policy=self._retry_policy(),
            error_prefix="LiteLLM",
            sleep=self._sleep,
            client=self._get_http_client(),
        )

    def analyze_image(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        filename: str = "",
        prompt: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> VisionAnalysisResult:
        if not mime_type.startswith("image/"):
            raise ValueError(f"Unsupported mime type for vision analysis: {mime_type}")

        encoded = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{encoded}"
        default_prompt = (self._settings.vision_default_prompt or "").strip() or DEFAULT_VISION_USER_PROMPT
        user_prompt = prompt or (
            f"{default_prompt}\n\n（文件名：{filename or 'unknown'}）"
        )
        resolved_model = (model or self._settings.vision_model or "").strip()
        if not resolved_model:
            raise RuntimeError("vision model is not configured")
        resolved_timeout = float(
            timeout if timeout is not None else self._settings.vision_timeout
        )

        body = self.chat_completions(
            model=resolved_model,
            messages=[
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            timeout=resolved_timeout,
        )
        content = _extract_message_content(body)
        return _coerce_vision_result(content)

    def analyze_image_with_prompt(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        prompt: str,
        filename: str = "",
    ) -> CustomMediaAnalysisResult:
        """用户自定义提示词的图片分析（自由文本/Markdown，非固定 JSON schema）。"""
        if not mime_type.startswith("image/"):
            raise ValueError(f"Unsupported mime type for image analysis: {mime_type}")
        if not prompt.strip():
            raise ValueError("prompt is required")

        encoded = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{encoded}"
        user_text = (
            f"{prompt.strip()}\n\n"
            f"（文件名：{filename or 'unknown'}）"
        )
        body = self.chat_completions(
            model=self._settings.vision_model,
            messages=[
                {"role": "system", "content": CUSTOM_IMAGE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            timeout=self._settings.vision_timeout,
        )
        content = _strip_outer_markdown_fence(_extract_message_content(body))
        return CustomMediaAnalysisResult(
            text=content,
            raw_response=content,
            model=self._settings.vision_model,
        )

    def analyze_audio_with_prompt(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str,
        prompt: str,
        filename: str = "",
    ) -> CustomMediaAnalysisResult:
        """用户自定义提示词的音频分析/转写（依赖网关侧支持 audio 的模型）。"""
        if not (mime_type.startswith("audio/") or _is_audio_filename(filename)):
            raise ValueError(f"Unsupported mime type for audio analysis: {mime_type}")
        if not prompt.strip():
            raise ValueError("prompt is required")

        audio_format = _audio_format_from_mime(mime_type, filename)
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        user_text = (
            f"{prompt.strip()}\n\n"
            f"（文件名：{filename or 'unknown'}，格式：{audio_format}）"
        )
        body = self.chat_completions(
            model=self._settings.audio_model,
            messages=[
                {"role": "system", "content": CUSTOM_AUDIO_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": encoded,
                                "format": audio_format,
                            },
                        },
                    ],
                },
            ],
            timeout=self._settings.audio_timeout,
        )
        content = _strip_outer_markdown_fence(_extract_message_content(body))
        return CustomMediaAnalysisResult(
            text=content,
            raw_response=content,
            model=self._settings.audio_model,
        )

    def analyze_video_with_prompt(
        self,
        video_bytes: bytes,
        *,
        mime_type: str,
        prompt: str,
        filename: str = "",
    ) -> CustomMediaAnalysisResult:
        """用户自定义提示词的视频分析（依赖网关侧支持 video/file 的模型）。

        使用 LiteLLM/OpenAI 兼容的 ``file`` 内容块 + base64 data URL，
        适配 Gemini 等原生支持视频的模型；内网切换 ``video_model`` 即可。
        """
        if not (mime_type.startswith("video/") or _is_video_filename(filename)):
            raise ValueError(f"Unsupported mime type for video analysis: {mime_type}")
        if not prompt.strip():
            raise ValueError("prompt is required")

        video_mime = _video_mime_from_inputs(mime_type, filename)
        encoded = base64.b64encode(video_bytes).decode("ascii")
        data_url = f"data:{video_mime};base64,{encoded}"
        user_text = (
            f"{prompt.strip()}\n\n"
            f"（文件名：{filename or 'unknown'}，MIME：{video_mime}）"
        )
        body = self.chat_completions(
            model=self._settings.video_model,
            messages=[
                {"role": "system", "content": CUSTOM_VIDEO_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "file",
                            "file": {
                                "file_id": data_url,
                                "format": video_mime,
                            },
                        },
                    ],
                },
            ],
            timeout=self._settings.video_timeout,
        )
        content = _strip_outer_markdown_fence(_extract_message_content(body))
        return CustomMediaAnalysisResult(
            text=content,
            raw_response=content,
            model=self._settings.video_model,
        )


_client: LiteLLMClient | None = None


def get_litellm_client() -> LiteLLMClient:
    global _client
    if _client is None:
        _client = LiteLLMClient()
    return _client


def set_litellm_client(client: LiteLLMClient | None) -> None:
    global _client
    _client = client


def _extract_message_content(body: dict) -> str:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected LiteLLM response shape: {body}") from exc

    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part).strip()
    raise ValueError(f"Unsupported message content type: {type(content)}")


def _strip_outer_markdown_fence(raw: str) -> str:
    """去掉模型偶发的整篇 ```markdown ... ``` 包裹，保留正文 Markdown。"""
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```(?:markdown|md|json)?\s*\n?", "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _parse_json_payload(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {
            "description": text,
            "objects": [],
            "scene": "",
            "visible_text": [],
            "confidence": None,
        }
    if not isinstance(data, dict):
        return {"description": text, "objects": [], "scene": "", "visible_text": [], "confidence": None}
    return data


def _looks_like_legacy_vision_json(data: dict) -> bool:
    """旧版视觉 JSON schema：description / visible_text / objects / scene。"""
    keys = set(data.keys())
    return bool(keys & {"description", "visible_text", "objects", "scene", "confidence"})


def markdown_from_legacy_vision_fields(
    *,
    description: str,
    scene: str,
    visible_text: list[str],
) -> str:
    """将旧版 JSON 字段合成 Markdown（兼容与单测）。"""
    md_parts: list[str] = []
    if description:
        md_parts.append(description.strip())
    if scene:
        md_parts.append(f"**场景**：{scene.strip()}")
    if visible_text:
        md_parts.append("## 识别文本")
        md_parts.extend(f"- {line}" for line in visible_text if str(line).strip())
    return "\n\n".join(part for part in md_parts if part).strip()


def _coerce_vision_result(content: str) -> VisionAnalysisResult:
    """将模型输出规范为 VisionAnalysisResult。

    默认期望 Markdown；若模型仍返回旧 JSON schema 则兼容解析并合成 Markdown。
    """
    raw = (content or "").strip()
    if not raw:
        return VisionAnalysisResult(markdown="", description="", raw_response="")

    # 尝试 JSON（含 fenced ```json）
    candidate = raw
    if candidate.startswith("```"):
        fenced = re.sub(r"^```(?:json)?\s*", "", candidate)
        fenced = re.sub(r"\s*```$", "", fenced).strip()
        candidate = fenced
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict) and _looks_like_legacy_vision_json(data):
        description = str(data.get("description", "")).strip()
        scene = str(data.get("scene", "")).strip()
        visible_text = _as_string_list(data.get("visible_text"))
        objects = _as_string_list(data.get("objects"))
        confidence = _as_confidence(data.get("confidence"))
        markdown = markdown_from_legacy_vision_fields(
            description=description,
            scene=scene,
            visible_text=visible_text,
        )
        return VisionAnalysisResult(
            markdown=markdown,
            description=description or markdown,
            objects=objects,
            scene=scene,
            visible_text=visible_text,
            confidence=confidence,
            raw_response=raw,
        )

    markdown = _strip_outer_markdown_fence(raw)
    return VisionAnalysisResult(
        markdown=markdown,
        description=markdown,
        raw_response=raw,
    )


def _as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_confidence(value: object) -> float | None:
    if value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence < 0:
        return 0.0
    if confidence > 1:
        return 1.0
    return confidence


def _is_audio_filename(filename: str) -> bool:
    name = (filename or "").lower()
    return any(name.endswith(ext) for ext in (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".wma"))


def _is_video_filename(filename: str) -> bool:
    name = (filename or "").lower()
    return any(
        name.endswith(ext)
        for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", ".mpeg", ".mpg", ".m4v", ".3gp")
    )


def _video_mime_from_inputs(mime_type: str, filename: str = "") -> str:
    mime = (mime_type or "").lower().strip()
    if mime.startswith("video/"):
        return mime
    name = (filename or "").lower()
    mapping = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
        ".mpeg": "video/mpeg",
        ".mpg": "video/mpeg",
        ".3gp": "video/3gpp",
    }
    for ext, value in mapping.items():
        if name.endswith(ext):
            return value
    return "video/mp4"


def _audio_format_from_mime(mime_type: str, filename: str = "") -> str:
    mime = (mime_type or "").lower()
    mapping = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/wave": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mp4": "mp4",
        "audio/m4a": "mp4",
        "audio/x-m4a": "mp4",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
        "audio/aac": "aac",
    }
    if mime in mapping:
        return mapping[mime]
    name = (filename or "").lower()
    for ext, fmt in (
        (".wav", "wav"),
        (".mp3", "mp3"),
        (".m4a", "mp4"),
        (".ogg", "ogg"),
        (".flac", "flac"),
        (".aac", "aac"),
    ):
        if name.endswith(ext):
            return fmt
    return "wav"