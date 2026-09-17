"""图片 / 音频 / 视频自定义 prompt 解析 API。"""
from unittest.mock import MagicMock

import pytest

from app.integrations.litellm.client import CustomMediaAnalysisResult
from tests.conftest import upload_bytes


@pytest.mark.parametrize(
    (
        "filename",
        "payload",
        "mime",
        "mock_attr",
        "mock_result",
        "prompt",
        "media_kind",
        "needle",
        "explicit_kind",
        "check_list",
    ),
    [
        (
            "receipt.png",
            b"\x89PNG\r\n\x1a\n",
            "image/png",
            "analyze_image_with_prompt",
            CustomMediaAnalysisResult(
                text="发票号码：INV-001\n金额：100.00",
                raw_response="发票号码：INV-001\n金额：100.00",
                model="qwen3.6-flash",
            ),
            "请提取发票号码和金额",
            "image",
            ("INV-001",),
            True,
            True,
        ),
        (
            "meeting.wav",
            b"RIFF....WAVEfmt ",
            "audio/wav",
            "analyze_audio_with_prompt",
            CustomMediaAnalysisResult(
                text="会议纪要：讨论 Q3 目标",
                model="gpt-4o-audio-preview",
            ),
            "请转写并写会议纪要",
            "audio",
            ("会议纪要",),
            False,
            False,
        ),
        (
            "demo.mp4",
            b"\x00\x00\x00\x18ftypisom",
            "video/mp4",
            "analyze_video_with_prompt",
            CustomMediaAnalysisResult(
                text="画面：产品演示；旁白概括核心功能。",
                model="gemini-flash-3.5",
            ),
            "请概述画面与旁白",
            "video",
            ("产品", "演示"),
            False,
            False,
        ),
    ],
)
def test_custom_parse_media_with_prompt(
    client,
    auth_headers,
    monkeypatch,
    filename,
    payload,
    mime,
    mock_attr,
    mock_result,
    prompt,
    media_kind,
    needle,
    explicit_kind,
    check_list,
):
    test_client, _ = client
    mock_client = MagicMock()
    getattr(mock_client, mock_attr).return_value = mock_result
    monkeypatch.setattr(
        "app.services.custom_parse_service.get_litellm_client",
        lambda: mock_client,
    )

    upload = upload_bytes(
        client, auth_headers, payload, name=filename, mime=mime, auto_parse="false"
    )
    assert upload.status_code == 201
    body = upload.json()
    assert body["auto_parse_applied"] is False
    reference_id = body["reference_id"]

    create_body = {"prompt": prompt}
    if explicit_kind:
        create_body["media_kind"] = media_kind
        assert body["auto_parse_skip_reason"] in {"user_disabled", "disabled_by_config"}

    create = test_client.post(
        f"/v1/files/{reference_id}/custom-parse",
        headers=auth_headers,
        json=create_body,
    )
    assert create.status_code == 202
    job = create.json()
    assert job["status"] == "completed"
    assert job["media_kind"] == media_kind
    assert any(token in (job["result_text"] or "") for token in needle)
    if explicit_kind:
        assert job["prompt"] == prompt
        getattr(mock_client, mock_attr).assert_called_once()
        call_kwargs = getattr(mock_client, mock_attr).call_args
        assert call_kwargs.kwargs["prompt"] == prompt

    if check_list:
        job_id = job["job_id"]
        detail = test_client.get(
            f"/v1/files/{reference_id}/custom-parse/{job_id}",
            headers=auth_headers,
        )
        assert detail.status_code == 200
        assert detail.json()["job_id"] == job_id
        assert detail.json()["status"] == "completed"

        listed = test_client.get(
            f"/v1/files/{reference_id}/custom-parse",
            headers=auth_headers,
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["jobs"][0]["job_id"] == job_id


def test_custom_parse_rejects_text_file(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(
        client, auth_headers, b"hello", name="notes.txt", auto_parse="false"
    )
    reference_id = upload.json()["reference_id"]
    resp = test_client.post(
        f"/v1/files/{reference_id}/custom-parse",
        headers=auth_headers,
        json={"prompt": "总结一下"},
    )
    assert resp.status_code == 400


def test_two_custom_prompts_create_two_jobs(client, auth_headers, monkeypatch):
    test_client, _ = client
    mock_client = MagicMock()
    mock_client.analyze_image_with_prompt.side_effect = [
        CustomMediaAnalysisResult(text="描述A", model="m"),
        CustomMediaAnalysisResult(text="描述B", model="m"),
    ]
    monkeypatch.setattr(
        "app.services.custom_parse_service.get_litellm_client",
        lambda: mock_client,
    )
    upload = upload_bytes(
        client,
        auth_headers,
        b"\x89PNG\r\n",
        name="a.png",
        mime="image/png",
        auto_parse="false",
    )
    reference_id = upload.json()["reference_id"]
    j1 = test_client.post(
        f"/v1/files/{reference_id}/custom-parse",
        headers=auth_headers,
        json={"prompt": "描述场景"},
    ).json()
    j2 = test_client.post(
        f"/v1/files/{reference_id}/custom-parse",
        headers=auth_headers,
        json={"prompt": "提取文字"},
    ).json()
    assert j1["job_id"] != j2["job_id"]
    assert j1["result_text"] == "描述A"
    assert j2["result_text"] == "描述B"
    listed = test_client.get(f"/v1/files/{reference_id}/custom-parse", headers=auth_headers)
    assert listed.json()["total"] == 2


def test_upload_text_still_auto_parses(client, auth_headers):
    upload = upload_bytes(client, auth_headers, b"# Title\nbody", name="doc.txt")
    assert upload.status_code == 201
    body = upload.json()
    assert body["auto_parse_requested"] is True
    assert body["auto_parse_applied"] is True
    assert body["parse_status"] == "completed"
