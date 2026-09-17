"""智能模式：面向智能体的 Markdown 后处理，去掉无信息量噪音。

当前能力：
- 电子表格：丢弃无实质内容的 sheet 段（Excel 默认空白 Sheet2/Sheet3 等）
"""
from __future__ import annotations

import re
from typing import Any

# MarkItDown 对 xls/xlsx 的 sheet 标题形态：## Sheet1
_SHEET_SPLIT = re.compile(r"(?m)(?=^## )")
# 仅由 |、-、:、空白构成的行（空表行或对齐分隔行）
_EMPTY_OR_SEP_ROW = re.compile(r"^[\s|:\-]*$")

_SPREADSHEET_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.ms-excel.sheet.macroEnabled.12",
        "text/csv",
        "application/csv",
    }
)
_SPREADSHEET_SUFFIXES = frozenset({".xls", ".xlsx", ".xlsm", ".xlsb", ".csv"})


def is_spreadsheet(*, mime_type: str | None = None, filename: str | None = None) -> bool:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime in _SPREADSHEET_MIME_TYPES:
        return True
    if mime in {"application/octet-stream", ""} and filename:
        lower = filename.lower()
        return any(lower.endswith(suffix) for suffix in _SPREADSHEET_SUFFIXES)
    if filename:
        lower = filename.lower()
        return any(lower.endswith(suffix) for suffix in _SPREADSHEET_SUFFIXES)
    return False


def filter_empty_spreadsheet_sheets(markdown: str) -> str:
    """去掉没有实质单元格内容的 ``## Sheet`` 段。

    MarkItDown 对空白 sheet 典型输出::

        ## Sheet2
        |
        |  |
    """
    if not markdown or "## " not in markdown:
        return markdown

    parts = _SHEET_SPLIT.split(markdown)
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if not part.startswith("## "):
            # 标题前的前言（少见），有内容则保留
            if part.strip():
                kept.append(part.rstrip())
            continue
        lines = part.splitlines()
        body = "\n".join(lines[1:])
        if _sheet_body_is_empty(body):
            continue
        kept.append(part.rstrip())

    if not kept:
        return ""
    return "\n\n".join(kept) + "\n"


def _sheet_body_is_empty(body: str) -> bool:
    text = body.strip()
    if not text:
        return True
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _EMPTY_OR_SEP_ROW.fullmatch(line):
            continue
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            for cell in cells:
                if cell and not re.fullmatch(r"[:\-]+", cell):
                    return False
            continue
        # 非表格正文
        return False
    return True


def apply_smart_markdown(
    markdown: str,
    *,
    smart_mode: bool = True,
    mime_type: str | None = None,
    filename: str | None = None,
) -> str:
    """按智能模式后处理 Markdown；关闭或非表格类型时原样返回。"""
    if not smart_mode or not markdown:
        return markdown
    if not is_spreadsheet(mime_type=mime_type, filename=filename):
        return markdown
    return filter_empty_spreadsheet_sheets(markdown)


def apply_smart_mode_to_content(
    content: Any,
    *,
    smart_mode: bool = True,
) -> Any:
    """对 StructuredContent 中的 markdown 块应用智能模式（幂等）。"""
    if not smart_mode:
        return content

    metadata = dict(content.metadata or {})
    mime_type = metadata.get("mime_type")
    filename = metadata.get("filename")
    if not is_spreadsheet(mime_type=mime_type, filename=filename):
        # 元数据缺失时：若 markdown 呈 sheet 分段形态仍尝试过滤
        raw = metadata.get("markdown")
        if not isinstance(raw, str) or not re.search(r"(?m)^## .+$", raw):
            return content
        # 有 ## 分段但不知类型时，仅当过滤后有变化且不像误伤正文时再应用
        # 保守：无 mime/filename 时不处理
        return content

    raw_markdown = metadata.get("markdown")
    if not isinstance(raw_markdown, str):
        # 回退 blocks
        if content.blocks and content.blocks[0].text:
            raw_markdown = content.blocks[0].text
        else:
            return content

    cleaned = apply_smart_markdown(
        raw_markdown,
        smart_mode=True,
        mime_type=mime_type,
        filename=filename,
    )
    if cleaned == raw_markdown:
        # 仍标注已评估，便于调用方感知
        if metadata.get("smart_mode") is True:
            return content
        metadata = {**metadata, "smart_mode": True}
        return content.model_copy(update={"metadata": metadata})

    metadata = {
        **metadata,
        "markdown": cleaned,
        "char_count": len(cleaned),
        "smart_mode": True,
    }
    # 统计被丢弃的 sheet（粗略：过滤前后 ## 标题数差）
    before = len(re.findall(r"(?m)^## .+$", raw_markdown))
    after = len(re.findall(r"(?m)^## .+$", cleaned))
    if before > after:
        metadata["smart_dropped_sheets"] = before - after

    blocks = list(content.blocks)
    if blocks and blocks[0].text is not None:
        blocks[0] = blocks[0].model_copy(update={"text": cleaned})

    return content.model_copy(update={"blocks": blocks, "metadata": metadata})
