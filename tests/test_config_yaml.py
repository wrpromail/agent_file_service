"""config.yaml 加载与 Settings 合并。"""
from pathlib import Path

from app.config import Settings, _flatten_yaml_config, load_yaml_config_dict, resolve_config_yaml_path


def test_flatten_auto_parse_nested():
    flat = _flatten_yaml_config(
        {
            "vision_model": "qwen3.5:cloud",
            "auto_parse": {
                "image": {
                    "enabled": True,
                    "profile": "deep",
                    "max_bytes": 123,
                },
                "pdf": {"enabled": False, "max_pages": 9},
            },
        }
    )
    assert flat["vision_model"] == "qwen3.5:cloud"
    assert flat["auto_parse_image_enabled"] is True
    assert flat["auto_parse_image_profile"] == "deep"
    assert flat["auto_parse_image_max_bytes"] == 123
    assert flat["auto_parse_pdf_enabled"] is False
    assert flat["auto_parse_pdf_max_pages"] == 9


def test_repo_config_yaml_resolves():
    path = resolve_config_yaml_path()
    assert path is not None
    assert path.name == "config.yaml"
    data = load_yaml_config_dict(path)
    assert data.get("vision_model") == "qwen3.5:cloud"
    assert data.get("auto_parse_image_enabled") is True
    assert data.get("auto_parse_image_profile") == "layout"
    assert data.get("video_model") == "gemini-flash-3.5"
    assert data.get("auto_parse_video_enabled") is True
    assert data.get("auto_parse_video_profile") == "layout"
    assert data.get("max_upload_bytes") == 20971520  # 20 MiB（与仓库 config.yaml 一致）
    assert data.get("outbound_http_max_attempts") == 3
    assert data.get("mineru_max_attempts") == 5
    assert data.get("auto_parse_spreadsheet_also_tables") is True
    assert data.get("auto_parse_spreadsheet_tables_max_rows") == 5000
    assert data.get("auto_parse_spreadsheet_tables_max_cols") == 64
    assert data.get("convert_compat_allow_private_urls") is True
    assert data.get("convert_compat_timeout") == 300.0


def test_settings_init_overrides_yaml(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "vision_model: from-yaml\nauto_parse:\n  image:\n    enabled: true\n    profile: deep\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    # 构造参数优先于 yaml
    s = Settings(vision_model="from-init")
    assert s.vision_model == "from-init"
