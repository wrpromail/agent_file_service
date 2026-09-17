import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.integrations.mineru.client import MinerUClient
from app.integrations.mineru.mapper import map_content_list_to_structured


@pytest.fixture()
def mineru_settings(tmp_path: Path) -> Settings:
    return Settings(
        mineru_api_token="test-token",
        mineru_base_url="https://mineru.example/api/v4",
        mineru_work_dir=str(tmp_path),
    )


def test_map_content_list_to_structured(tmp_path: Path):
    content_list = [
        {"type": "text", "text": "# 合同标的", "page_idx": 0, "score": 0.98},
        {
            "type": "table",
            "table_body": "<table><tr><td>项目</td><td>金额</td></tr><tr><td>服务费</td><td>10000</td></tr></table>",
            "page_idx": 2,
            "score": 0.91,
        },
    ]
    (tmp_path / "demo_content_list.json").write_text(
        json.dumps(content_list, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "full.md").write_text("# 合同标的\n\n<table></table>\n", encoding="utf-8")

    structured = map_content_list_to_structured(
        content_hash="abc123",
        parsed_dir=tmp_path,
        parser_name="mineru",
        parser_version="0.1.0",
        profile="layout",
    )

    assert structured.source.parser_name == "mineru"
    assert structured.blocks[0].type == "heading"
    assert structured.blocks[0].page == 1
    assert structured.tables[0].page == 3
    assert structured.tables[0].rows == [["项目", "金额"], ["服务费", "10000"]]
    assert structured.metadata.get("markdown") == "# 合同标的\n\n<table></table>\n"
    assert structured.metadata.get("format") == "markdown"


def test_run_batch_downloads_zip(mineru_settings: Settings, tmp_path: Path):
    file_path = tmp_path / "demo.pdf"
    file_path.write_bytes(b"%PDF-1.4 demo")

    client = MinerUClient(mineru_settings)
    zip_bytes = b"PK\x03\x04demo"

    create_response = {
        "code": 0,
        "data": {"batch_id": "batch-1", "file_urls": ["https://upload.example/put"]},
    }
    poll_response = {
        "data": {
            "extract_result": [
                {
                    "data_id": "abcd1234.pdf",
                    "file_name": "demo.pdf",
                    "state": "done",
                    "full_zip_url": "https://download.example/result.zip",
                }
            ]
        }
    }

    mock_session = MagicMock()
    mock_put = MagicMock()
    mock_put.status_code = 200
    mock_session.put.return_value = mock_put

    mock_zip = MagicMock()
    mock_zip.status_code = 200
    mock_zip.iter_content.return_value = [zip_bytes]
    mock_zip.__enter__.return_value = mock_zip
    mock_zip.__exit__.return_value = None
    mock_session.get.return_value = mock_zip

    client._session = mock_session

    with patch.object(client, "_request_json", side_effect=[create_response, poll_response]):
        with patch.object(client, "_inspect_zip", return_value={"has_full_md": True, "full_md_path": "full.md"}):
            result = client.run_batch(
                file_path,
                doc_id="abcd1234",
                output_root=tmp_path / "runs",
            )

    assert result.batch_id == "batch-1"
    assert result.records[0]["state"] == "done"
    assert Path(result.records[0]["downloaded_zip_path"]).exists()