"""Markdown → HTML → PDF（LibreOffice）。file_convert 兼容层用，不引入 wkhtmltopdf。"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from app.config import Settings, get_settings
from app.parsers.legacy_office import convert_with_libreoffice, resolve_soffice_binary

logger = logging.getLogger(__name__)

_HTML_SHELL = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    font-family: "WenQuanYi Zen Hei", "Microsoft YaHei", sans-serif;
    line-height: 1.6;
    margin: 20px;
    color: #333;
}}
h1, h2, h3, h4, h5, h6 {{
    font-weight: bold;
    margin-top: 1.2em;
    margin-bottom: 0.6em;
    page-break-after: avoid;
}}
p {{ margin: 0.8em 0; }}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 1em auto;
    font-size: 14px;
    page-break-inside: avoid;
}}
th, td {{
    border: 1px solid #333;
    padding: 8px 12px;
    text-align: center;
    vertical-align: middle;
}}
th {{ background-color: #f2f2f2; font-weight: bold; }}
strong, b {{ font-weight: bold !important; }}
img {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
hr {{ border: none; border-top: 1px solid #333; margin: 1em 0; }}
pre {{
    background-color: #f5f5f5;
    padding: 10px;
    border: 1px solid #ccc;
    overflow-x: auto;
}}
code {{ font-family: "DejaVu Sans Mono", monospace; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def markdown_to_html(markdown_content: str) -> str:
    try:
        import markdown as md_lib
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "markdown is not installed; install with: pip install 'file-service[parsers]'"
        ) from exc
    body = md_lib.markdown(
        markdown_content,
        extensions=["tables", "fenced_code", "md_in_html", "extra"],
    )
    return _HTML_SHELL.format(body=body)


def convert_markdown_to_pdf(
    markdown_content: str,
    *,
    settings: Settings | None = None,
) -> bytes:
    """Render Markdown to PDF bytes via HTML + LibreOffice."""
    settings = settings or get_settings()
    html = markdown_to_html(markdown_content)
    binary = resolve_soffice_binary(settings)
    if not binary:
        raise RuntimeError(
            "LibreOffice (soffice) is not available for Markdown to PDF. "
            "Install LibreOffice or set LEGACY_OFFICE_BINARY / legacy_office_binary."
        )
    work_dir = tempfile.mkdtemp(prefix="fs-md-pdf-")
    try:
        html_path = Path(work_dir) / "input.html"
        html_path.write_text(html, encoding="utf-8")
        pdf_path = convert_with_libreoffice(
            str(html_path),
            "pdf",
            output_dir=work_dir,
            binary=binary,
            timeout=int(settings.convert_compat_timeout or settings.legacy_office_timeout or 300),
        )
        return Path(pdf_path).read_bytes()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
