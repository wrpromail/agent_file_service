from pathlib import Path
from unittest.mock import MagicMock

from app.config import Settings
from app.integrations.litellm.client import VisionAnalysisResult
from app.parsers.base import ParseContext
from app.parsers.vision_deep import VisionDeepParser


def test_vision_deep_parser_maps_structured_content(tmp_path: Path):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"\x89PNG\r\n")

    mock_client = MagicMock()
    mock_client.analyze_image.return_value = VisionAnalysisResult(
        markdown="## 桌面截图\n\n操作系统界面\n\n- 确定",
        description="桌面截图",
        objects=["窗口", "按钮"],
        scene="操作系统界面",
        visible_text=["确定"],
        confidence=0.91,
    )

    parser = VisionDeepParser(client=mock_client, settings=Settings())
    context = ParseContext(
        content_hash="abc123",
        mime_type="image/png",
        filename="sample.png",
        size_bytes=8,
        classification=None,
    )
    structured = parser.parse(str(image_path), context)

    assert structured.source.profile == "deep"
    assert "## 桌面截图" in structured.blocks[0].text
    assert structured.metadata["objects"] == ["窗口", "按钮"]
    assert "确定" in (structured.metadata.get("markdown") or "")
    assert structured.metadata.get("format") == "markdown"
    mock_client.analyze_image.assert_called_once()
