"""MinerU 结果 zip 解压与资产扫描（融合自 mineruapi/pipeline/parse.py）。"""
from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


def find_content_list_json(parsed_dir: Path) -> Path | None:
    matches = [
        path
        for path in parsed_dir.glob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    ]
    if matches:
        return matches[0]
    part_matches = [
        path
        for path in parsed_dir.glob("parts/*/*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    ]
    return part_matches[0] if part_matches else None


def scan_parsed_assets(parsed_dir: Path) -> dict[str, Any]:
    if not parsed_dir.exists():
        return {
            "layout_json": None,
            "content_list_json": None,
            "model_json": None,
            "image_count": 0,
        }

    layout_json = next((str(p.resolve()) for p in parsed_dir.glob("layout.json")), None)
    content_list_json = find_content_list_json(parsed_dir)
    model_json = next((str(p.resolve()) for p in parsed_dir.glob("*_model.json")), None)
    images_dir = parsed_dir / "images"
    image_count = len(list(images_dir.glob("*"))) if images_dir.exists() else 0
    return {
        "layout_json": layout_json,
        "content_list_json": str(content_list_json.resolve()) if content_list_json else None,
        "model_json": model_json,
        "image_count": image_count,
    }


def _safe_member_path(raw: str, out_dir: Path) -> Path | None:
    """Return a path under ``out_dir`` for ``raw``, or None if unsafe (zip slip)."""
    text = str(raw).replace("\\", "/").strip()
    if not text or text.endswith("/"):
        text = text.rstrip("/")
        if not text:
            return None
    path = PurePosixPath(text)
    if path.is_absolute() or str(path).startswith("/"):
        return None
    parts = path.parts
    if any(p in {"", ".", ".."} for p in parts):
        return None
    if any(p.startswith("..") for p in parts):
        return None
    dest = (out_dir / Path(*parts)).resolve()
    try:
        dest.relative_to(out_dir.resolve())
    except ValueError:
        return None
    return dest


def extract_zip_full(zip_path: Path, out_dir: Path) -> dict[str, Any]:
    if not zip_path.exists():
        raise FileNotFoundError(f"zip not found: {zip_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_resolved = out_dir.resolve()
    extracted_files: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename.endswith("/"):
                dest = _safe_member_path(info.filename, out_resolved)
                if dest is not None:
                    dest.mkdir(parents=True, exist_ok=True)
                continue
            dest = _safe_member_path(info.filename, out_resolved)
            if dest is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(dest, "wb") as fh:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            extracted_files.append(info.filename)

    return {
        "status": "extracted",
        "out_dir": str(out_resolved),
        "file_count": len(extracted_files),
        "assets": scan_parsed_assets(out_dir),
    }
