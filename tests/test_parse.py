from unittest.mock import MagicMock

from app.integrations.litellm.client import VisionAnalysisResult
from tests.conftest import reload_parsers, upload_bytes


def test_upload_auto_parse_returns_content(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(
        client, auth_headers, b"# Title\n\nBody paragraph", name="doc.txt"
    )
    reference_id = upload.json()["reference_id"]

    content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    )
    assert content.status_code == 200
    body = content.json()
    assert body["source"]["parser_name"] == "simple"
    assert len(body["blocks"]) == 2
    assert body["blocks"][0]["type"] == "heading"


def test_content_soft_not_ready_when_not_parsed(client, auth_headers):
    """方案 B：未解析时 content 仍 200，ready=false + 文案，不用 409。"""
    test_client, _ = client
    upload = upload_bytes(
        client,
        auth_headers,
        b"pending body",
        name="doc.txt",
        auto_parse="false",
    )
    reference_id = upload.json()["reference_id"]

    response = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["status"] == "not_started"
    assert body["message_code"] == "parse_not_started"
    assert body["message"]
    assert body["blocks"] == []
    assert body["metadata"].get("markdown") is None


def test_content_ready_envelope_when_completed(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(client, auth_headers, b"# Title\n\nBody", name="doc.txt")
    reference_id = upload.json()["reference_id"]
    response = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["status"] == "completed"
    assert body["message"] is None
    assert body["blocks"]


def test_trigger_parse_endpoint(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(
        client, auth_headers, b"manual parse", name="doc.txt", auto_parse="false"
    )
    reference_id = upload.json()["reference_id"]

    trigger = test_client.post(
        f"/v1/files/{reference_id}/parse",
        headers=auth_headers,
        json={"profile": "layout", "force": False},
    )
    assert trigger.status_code == 202
    assert trigger.json()["status"] == "completed"

    content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout&types=paragraph",
        headers=auth_headers,
    )
    assert content.status_code == 200
    assert all(block["type"] == "paragraph" for block in content.json()["blocks"])


def test_trigger_ocr_profile(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(
        client, auth_headers, b"ocr text", name="doc.txt", auto_parse="false"
    )
    reference_id = upload.json()["reference_id"]
    trigger = test_client.post(
        f"/v1/files/{reference_id}/parse",
        headers=auth_headers,
        json={"profile": "ocr", "force": False},
    )
    assert trigger.status_code == 202, trigger.text
    assert trigger.json()["profile"] == "ocr"
    content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=ocr",
        headers=auth_headers,
    )
    assert content.status_code == 200
    assert content.json()["ready"] is True


def test_content_max_chars_and_outline(client, auth_headers):
    test_client, _ = client
    body = b"# Title\n\n" + (b"word " * 200)
    upload = upload_bytes(client, auth_headers, body, name="long.txt")
    reference_id = upload.json()["reference_id"]

    clipped = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout&max_chars=20",
        headers=auth_headers,
    )
    assert clipped.status_code == 200
    meta = clipped.json()["metadata"]
    assert meta.get("truncated") is True
    assert meta.get("char_count") == 20

    outline = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout&mode=outline",
        headers=auth_headers,
    )
    assert outline.status_code == 200
    assert outline.json()["metadata"]["mode"] == "outline"


def test_deduplicated_upload_reuses_parse_result(client, auth_headers, other_tenant_headers):
    test_client, _ = client
    payload = b"# Shared\nshared content"

    first = upload_bytes(client, auth_headers, payload, name="a.txt")
    second = upload_bytes(client, other_tenant_headers, payload, name="b.txt")

    assert first.json()["parse_status"] == "completed"
    assert second.json()["parse_status"] == "completed"
    assert first.json()["content_hash"] == second.json()["content_hash"]

    detail = test_client.get(
        f"/v1/files/{second.json()['reference_id']}",
        headers=other_tenant_headers,
    )
    layout_results = [
        item for item in detail.json()["parse_results"] if item["profile"] == "layout"
    ]
    assert len(layout_results) == 1
    assert layout_results[0]["status"] == "completed"


def test_force_reparse_updates_content(client, auth_headers):
    test_client, _ = client
    upload = upload_bytes(client, auth_headers, b"# First", name="doc.txt")
    reference_id = upload.json()["reference_id"]

    first_content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    ).json()
    assert first_content["blocks"][0]["text"] == "First"

    trigger = test_client.post(
        f"/v1/files/{reference_id}/parse",
        headers=auth_headers,
        json={"profile": "layout", "force": True},
    )
    assert trigger.status_code == 202
    assert trigger.json()["status"] == "completed"

    second_content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=layout",
        headers=auth_headers,
    ).json()
    assert second_content["blocks"][0]["text"] == "First"


def test_trigger_deep_parse_for_image(client, auth_headers, monkeypatch):
    test_client, _ = client
    mock_client = MagicMock()
    mock_client.analyze_image.return_value = VisionAnalysisResult(
        markdown="蓝天白云",
        description="蓝天白云",
        objects=["天空", "云"],
        scene="户外",
        visible_text=[],
        confidence=0.95,
    )

    monkeypatch.setattr(
        "app.parsers.vision_deep.get_litellm_client",
        lambda: mock_client,
    )
    reload_parsers(
        monkeypatch,
        VISION_PARSER_NAME="qwen-vision",
        VISION_PARSER_VERSION="0.1.0",
    )

    upload = upload_bytes(
        client,
        auth_headers,
        b"\x89PNG\r\n",
        name="sky.png",
        mime="image/png",
        auto_parse="false",
    )
    assert upload.status_code == 201
    reference_id = upload.json()["reference_id"]

    trigger = test_client.post(
        f"/v1/files/{reference_id}/parse",
        headers=auth_headers,
        json={"profile": "deep", "force": False},
    )
    assert trigger.status_code == 202
    assert trigger.json()["status"] == "completed"

    content = test_client.get(
        f"/v1/files/{reference_id}/content?profile=deep",
        headers=auth_headers,
    )
    assert content.status_code == 200
    body = content.json()
    assert body["source"]["profile"] == "deep"
    assert body["blocks"][0]["text"] == "蓝天白云"
    assert body["metadata"]["objects"] == ["天空", "云"]


def test_force_parse_while_processing_returns_409(client, auth_headers, db_session):
    from sqlalchemy import select

    from app.models.parse_result import ParseResult

    test_client, _ = client
    upload = upload_bytes(client, auth_headers, b"# Force conflict", name="doc.txt")
    reference_id = upload.json()["reference_id"]
    content_hash = upload.json()["content_hash"]

    row = db_session.scalar(
        select(ParseResult).where(
            ParseResult.content_hash == content_hash,
            ParseResult.profile == "layout",
        )
    )
    assert row is not None
    row.status = "processing"
    db_session.commit()

    trigger = test_client.post(
        f"/v1/files/{reference_id}/parse",
        headers=auth_headers,
        json={"profile": "layout", "force": True},
    )
    assert trigger.status_code == 409
