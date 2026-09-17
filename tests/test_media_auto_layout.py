"""图片 / 音频 / 视频自动 layout（mock LiteLLM）。"""
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.integrations.litellm.client import CustomMediaAnalysisResult, VisionAnalysisResult
from app.parsers.classifiers import classify_file
from app.parsers.factory import create_processing_factory
from app.parsers.types import FileKind, ProcessingLevel
from tests.conftest import reload_parsers, upload_bytes


@pytest.mark.parametrize(
    ("filename", "mime", "sample", "kind", "parser_name"),
    [
        ("meeting.wav", "audio/wav", b"RIFF", FileKind.AUDIO, "litellm-audio"),
        ("clip.mp4", "video/mp4", b"\x00\x00\x00\x18ftyp", FileKind.VIDEO, "litellm-video"),
    ],
)
def test_factory_media_layout_uses_litellm(filename, mime, sample, kind, parser_name):
    factory = create_processing_factory(Settings(layout_parser_name="simple"))
    classification = classify_file(filename, mime, sample)
    assert classification.kind == kind
    parser = factory.resolve_parser(classification, ProcessingLevel.LAYOUT.value)
    assert parser is not None
    assert parser.name == parser_name
    assert parser.profile == "layout"


@pytest.mark.parametrize(
    (
        "kind",
        "env",
        "filename",
        "payload",
        "mime",
        "mock_attr",
        "mock_module",
        "mock_result",
        "parser_name",
        "needle",
    ),
    [
        (
            "audio",
            {
                "AUTO_PARSE_AUDIO_ENABLED": "true",
                "AUTO_PARSE_AUDIO_PROFILE": "layout",
                "AUDIO_PARSER_NAME": "litellm-audio",
                "AUDIO_PARSER_VERSION": "0.1.0",
            },
            "meeting.wav",
            b"RIFF....WAVEfmt ",
            "audio/wav",
            "analyze_audio_with_prompt",
            "app.parsers.audio_layout.get_litellm_client",
            CustomMediaAnalysisResult(
                text="会议纪要：讨论 Q3 目标与预算。",
                model="gemini-flash-3.5",
            ),
            "litellm-audio",
            ("Q3", "会议"),
        ),
        (
            "video",
            {
                "AUTO_PARSE_VIDEO_ENABLED": "true",
                "AUTO_PARSE_VIDEO_PROFILE": "layout",
                "VIDEO_PARSER_NAME": "litellm-video",
                "VIDEO_PARSER_VERSION": "0.1.0",
            },
            "demo.mp4",
            b"\x00\x00\x00\x18ftypisom",
            "video/mp4",
            "analyze_video_with_prompt",
            "app.parsers.video_layout.get_litellm_client",
            CustomMediaAnalysisResult(
                text="视频概述：演示产品功能与操作步骤。",
                model="gemini-flash-3.5",
            ),
            "litellm-video",
            ("产品", "演示"),
        ),
        (
            "image",
            {
                "AUTO_PARSE_IMAGE_ENABLED": "true",
                "AUTO_PARSE_IMAGE_PROFILE": "layout",
                "VISION_PARSER_NAME": "qwen-vision",
                "VISION_PARSER_VERSION": "0.1.0",
            },
            "invoice.png",
            b"\x89PNG\r\n\x1a\n",
            "image/png",
            "analyze_image",
            "app.parsers.vision_deep.get_litellm_client",
            VisionAnalysisResult(
                description="一张包含发票编号的白底单据截图",
                objects=["文本", "边框"],
                scene="文档",
                visible_text=["Invoice INV-2026-001", "Amount: 1280.00 CNY"],
                confidence=0.95,
                raw_response="{}",
            ),
            "qwen-vision",
            ("发票", "Invoice", "INV-2026"),
        ),
    ],
)
def test_upload_media_auto_layout(
    client,
    auth_headers,
    monkeypatch,
    kind,
    env,
    filename,
    payload,
    mime,
    mock_attr,
    mock_module,
    mock_result,
    parser_name,
    needle,
):
    reload_parsers(monkeypatch, **env)

    mock_client = MagicMock()
    getattr(mock_client, mock_attr).return_value = mock_result
    monkeypatch.setattr(mock_module, lambda: mock_client)

    test_client, _ = client
    upload = upload_bytes(client, auth_headers, payload, name=filename, mime=mime)
    assert upload.status_code == 201
    body = upload.json()
    assert body["auto_parse_applied"] is True
    assert body["parse_status"] == "completed"
    reference_id = body["reference_id"]

    getattr(mock_client, mock_attr).assert_called_once()
    if kind in {"audio", "video"}:
        call = getattr(mock_client, mock_attr).call_args
        prompt = call.kwargs.get("prompt") or ""
        assert prompt

    content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    )
    assert content.status_code == 200
    payload_json = content.json()
    assert payload_json["source"]["profile"] == "layout"
    assert payload_json["source"]["parser_name"] == parser_name
    md = (payload_json.get("metadata") or {}).get("markdown") or ""
    assert any(token in md for token in needle)
