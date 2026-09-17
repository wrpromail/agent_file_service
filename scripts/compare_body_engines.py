#!/usr/bin/env python3
"""对比 mineru 与 vision-pages 正文识别（直接调 parser，不经 HTTP）。

用法（在仓库根、已 source .env）::

    .venv/bin/python scripts/compare_body_engines.py path/to/a.pdf path/to/b.docx
    BODY_ENGINE_IMPORT_ROOT=/path/to/docs .venv/bin/python scripts/compare_body_engines.py

结果写入 ``/tmp/body_engine_compare/``。不内置任何业务样例路径。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 加载 .env（不打印密钥）
_env = ROOT / ".env"
if _env.is_file():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from app.config import Settings, get_settings  # noqa: E402
from app.parsers.base import ParseContext  # noqa: E402
from app.parsers.factory import set_processing_factory  # noqa: E402
from app.parsers.mineru_layout import MinerULayoutParser  # noqa: E402
from app.parsers.vision_pages_layout import VisionPagesLayoutParser  # noqa: E402

OUT_DIR = Path("/tmp/body_engine_compare")

MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _slug(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", name)[:80]


def _page_markers(md: str) -> list[int]:
    return [int(x) for x in re.findall(r"<!--\s*page\s+(\d+)\s*-->", md, flags=re.I)]


def _collect_samples() -> list[Path]:
    extra = [Path(p).expanduser() for p in sys.argv[1:] if p]
    if extra:
        return extra
    root = Path(os.environ.get("BODY_ENGINE_IMPORT_ROOT", "")).expanduser()
    if root.is_dir():
        return sorted(p for p in root.rglob("*") if p.suffix.lower() in MIME and p.is_file())
    return []


def run_one(
    *,
    engine: str,
    path: Path,
    settings: Settings,
    max_pages_note: str,
) -> dict:
    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
    ctx = ParseContext(
        content_hash=f"compare-{engine}-{_slug(path.stem)}",
        mime_type=mime,
        filename=path.name,
        size_bytes=path.stat().st_size,
        classification=None,
    )
    if engine == "mineru":
        parser = MinerULayoutParser()
    elif engine == "vision-pages":
        parser = VisionPagesLayoutParser(settings=settings)
    else:
        raise ValueError(engine)

    t0 = time.perf_counter()
    err = None
    structured = None
    try:
        structured = parser.parse(str(path), ctx)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    elapsed = time.perf_counter() - t0

    row: dict = {
        "engine": engine,
        "file": path.name,
        "path": str(path),
        "elapsed_sec": round(elapsed, 2),
        "error": err,
        "max_pages_note": max_pages_note,
    }
    if structured is None:
        return row

    md = (structured.metadata or {}).get("markdown") or ""
    markers = _page_markers(md)
    row.update(
        {
            "parser_name": structured.source.parser_name,
            "char_count": len(md),
            "block_count": len(structured.blocks or []),
            "table_count": len(structured.tables or []),
            "doc_page_count": getattr(structured.document, "page_count", None),
            "page_count_rendered": (structured.metadata or {}).get("page_count_rendered"),
            "concurrency": (structured.metadata or {}).get("concurrency"),
            "truncated": (structured.metadata or {}).get("truncated"),
            "via_libreoffice": (structured.metadata or {}).get("via_libreoffice"),
            "page_markers": markers,
            "page_order_ok": markers == sorted(markers) and markers == list(range(1, len(markers) + 1))
            if markers
            else None,
            "warnings": [w.detail for w in (structured.warnings or [])],
            "markdown_preview": md[:800],
        }
    )

    out_md = OUT_DIR / f"{_slug(path.stem)}.{engine}.md"
    out_md.write_text(md, encoding="utf-8")
    row["markdown_path"] = str(out_md)
    return row


def main() -> int:
    get_settings.cache_clear()
    set_processing_factory(None)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    max_pages = int(os.environ.get("COMPARE_MAX_PAGES", "6"))
    concurrency = int(os.environ.get("COMPARE_CONCURRENCY", "3"))

    base_kwargs = dict(
        litellm_base_url=os.environ.get("LITELLM_BASE_URL") or "http://localhost:4000",
        litellm_api_key=os.environ.get("LITELLM_API_KEY") or "sk-dev-key",
        mineru_api_token=os.environ.get("MINERU_API_TOKEN") or "",
        mineru_base_url=os.environ.get("MINERU_BASE_URL") or "https://mineru.net/api/v4",
        mineru_work_dir="/tmp/file-service-mineru-compare",
        vision_pages_model=os.environ.get("VISION_PAGES_MODEL") or "kimi-k2.6:cloud",
        vision_pages_max_pages=max_pages,
        vision_pages_concurrency=concurrency,
        vision_pages_render_scale=1.5,
        vision_pages_max_edge_px=1400,
        vision_pages_timeout=180,
        vision_pages_work_dir="/tmp/file-service-vision-pages-compare",
        vision_pages_fail_fast=False,
        startup_dependency_check=False,
    )
    settings = Settings(**base_kwargs)

    samples = _collect_samples()
    missing = [p for p in samples if not p.is_file()]
    if missing:
        print("MISSING samples:", *missing, sep="\n  ")
        return 2
    if not samples:
        print(
            "usage: scripts/compare_body_engines.py <file.pdf|docx> [...]\n"
            "   or: BODY_ENGINE_IMPORT_ROOT=/path/to/docs scripts/compare_body_engines.py"
        )
        return 2

    print(
        f"samples={len(samples)} max_pages={max_pages} concurrency={concurrency} "
        f"model={settings.vision_pages_model}"
    )
    print(f"out={OUT_DIR}")

    rows: list[dict] = []
    for path in samples:
        for engine in ("mineru", "vision-pages"):
            note = (
                f"vision max_pages={max_pages}"
                if engine == "vision-pages"
                else "mineru full document"
            )
            print(f"\n=== {engine} :: {path.name} ===")
            row = run_one(engine=engine, path=path, settings=settings, max_pages_note=note)
            rows.append(row)
            if row.get("error"):
                print(f"  ERROR {row['error']} ({row['elapsed_sec']}s)")
            else:
                print(
                    f"  ok {row['elapsed_sec']}s chars={row.get('char_count')} "
                    f"blocks={row.get('block_count')} pages_rendered={row.get('page_count_rendered')} "
                    f"markers={row.get('page_markers')} order_ok={row.get('page_order_ok')}"
                )
                if row.get("warnings"):
                    print(f"  warnings={row['warnings'][:3]}")

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsummary -> {summary_path}")
    return 0 if not any(r.get("error") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
