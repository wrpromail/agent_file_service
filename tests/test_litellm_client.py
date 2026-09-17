import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.integrations.litellm.client import (
    LiteLLMClient,
    VISION_SYSTEM_PROMPT,
    _coerce_vision_result,
    _parse_json_payload,
    _strip_outer_markdown_fence,
)


@pytest.fixture()
def litellm_settings() -> Settings:
    return Settings(
        litellm_base_url="http://litellm.example",
        litellm_api_key="sk-test",
        vision_model="qwen3.5:cloud",
        vision_timeout=30,
        vision_default_prompt="请将图片中的可见文本还原为结构清晰的 Markdown 文档。",
    )


def test_parse_json_payload_accepts_fenced_json():
    raw = '```json\n{"description":"猫","objects":["猫"],"scene":"室内","visible_text":[],"confidence":0.9}\n```'
    parsed = _parse_json_payload(raw)
    assert parsed["description"] == "猫"
    assert parsed["objects"] == ["猫"]


def test_strip_outer_markdown_fence():
    raw = "```markdown\n# 标题\n\n正文\n```"
    assert _strip_outer_markdown_fence(raw) == "# 标题\n\n正文"


def test_coerce_vision_result_markdown_primary():
    result = _coerce_vision_result("## 发票\n\n| 项目 | 金额 |\n| --- | --- |\n| A | 1 |")
    assert result.markdown.startswith("## 发票")
    assert "金额" in result.markdown
    assert result.description == result.markdown


def test_coerce_vision_result_legacy_json():
    raw = json.dumps(
        {
            "description": "一只橘猫",
            "objects": ["猫"],
            "scene": "窗台",
            "visible_text": ["Hello"],
            "confidence": 0.88,
        },
        ensure_ascii=False,
    )
    result = _coerce_vision_result(raw)
    assert result.description == "一只橘猫"
    assert result.objects == ["猫"]
    assert result.visible_text == ["Hello"]
    assert "Hello" in result.markdown
    assert "一只橘猫" in result.markdown


def _mock_httpx_client(response_body: dict):
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = json.dumps(response_body)
    mock_response.json.return_value = response_body
    mock_response.raise_for_status = MagicMock()

    mock_http = MagicMock(spec=httpx.Client)
    mock_http.request.return_value = mock_response
    mock_http.close = MagicMock()
    return mock_http, mock_response


def test_analyze_image_calls_chat_completions(litellm_settings: Settings):
    client = LiteLLMClient(litellm_settings, sleep=lambda _s: None)
    md = "## 界面\n\n- 确定\n- 取消"
    response_body = {"choices": [{"message": {"content": md}}]}
    mock_http, _ = _mock_httpx_client(response_body)

    with patch("app.integrations.http.httpx_json.httpx.Client", return_value=mock_http):
        result = client.analyze_image(b"\x89PNG\r\n", mime_type="image/png", filename="cat.png")

    assert result.markdown == md
    assert "确定" in result.markdown

    request = mock_http.request.call_args
    assert request.args[0] == "POST"
    assert request.args[1] == "http://litellm.example/v1/chat/completions"
    payload = request.kwargs["json"]
    assert payload["model"] == "qwen3.5:cloud"
    assert payload["messages"][0]["content"] == VISION_SYSTEM_PROMPT
    assert "Markdown" in VISION_SYSTEM_PROMPT
    user_content = payload["messages"][1]["content"]
    assert "Markdown" in user_content[0]["text"]
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_analyze_image_rejects_non_image_mime(litellm_settings: Settings):
    client = LiteLLMClient(litellm_settings)
    with pytest.raises(ValueError, match="Unsupported mime type"):
        client.analyze_image(b"hello", mime_type="text/plain")


def test_analyze_image_retries_on_503(litellm_settings: Settings):
    litellm_settings.outbound_http_max_attempts = 3
    litellm_settings.outbound_http_backoff_base = 0.0
    client = LiteLLMClient(litellm_settings, sleep=lambda _s: None)
    ok_body = {"choices": [{"message": {"content": "## ok"}}]}

    fail = MagicMock(spec=httpx.Response)
    fail.status_code = 503
    fail.text = "unavailable"
    fail.json.side_effect = ValueError("not json")

    ok = MagicMock(spec=httpx.Response)
    ok.status_code = 200
    ok.text = json.dumps(ok_body)
    ok.json.return_value = ok_body

    mock_http = MagicMock(spec=httpx.Client)
    mock_http.request.side_effect = [fail, ok]
    mock_http.close = MagicMock()

    with patch("app.integrations.http.httpx_json.httpx.Client", return_value=mock_http):
        result = client.analyze_image(b"\x89PNG\r\n", mime_type="image/png", filename="x.png")

    assert "## ok" in result.markdown
    assert mock_http.request.call_count == 2


def test_analyze_video_with_prompt_uses_file_part():
    settings = Settings(
        litellm_base_url="http://litellm.example",
        litellm_api_key="sk-test",
        video_model="gemini-flash-3.5",
        video_timeout=60,
        outbound_http_max_attempts=1,
    )
    client = LiteLLMClient(settings, sleep=lambda _s: None)
    response_body = {"choices": [{"message": {"content": "画面是产品演示。"}}]}
    mock_http, _ = _mock_httpx_client(response_body)

    with patch("app.integrations.http.httpx_json.httpx.Client", return_value=mock_http):
        result = client.analyze_video_with_prompt(
            b"fake-mp4",
            mime_type="video/mp4",
            prompt="请概述视频",
            filename="demo.mp4",
        )

    assert "产品演示" in result.text
    assert result.model == "gemini-flash-3.5"
    payload = mock_http.request.call_args.kwargs["json"]
    assert payload["model"] == "gemini-flash-3.5"
    parts = payload["messages"][1]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "file"
    assert parts[1]["file"]["format"] == "video/mp4"
    assert parts[1]["file"]["file_id"].startswith("data:video/mp4;base64,")