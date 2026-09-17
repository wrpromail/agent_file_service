"""vision-pages：PDF/Office/表格的**正文识别**后端。

流程：页图栅格化 → LiteLLM 多模态逐页识别（可并发）→ 按页序拼接 Markdown 正文。
结果写入 API 的 ``profile=layout`` 缓存槽（历史名；语义是基础正文，不是「版面引擎」）。
主交付物与 markitdown 对齐：``metadata.markdown``。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.integrations.litellm.client import get_litellm_client
from app.parsers.base import ParseContext, Parser
from app.parsers.document_rasterize import PageImage, render_document_to_page_images
from app.schemas.content import (
    ContentBlock,
    ContentSource,
    ContentWarning,
    DocumentInfo,
    StructuredContent,
)

logger = logging.getLogger(__name__)

_SUPPORTED_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/rtf",
}


class VisionPagesLayoutParser(Parser):
    """正文识别：文档转图 + 多模态 → Markdown（落盘 profile=layout）。"""

    profile = "layout"
    supported_mime_types = list(_SUPPORTED_MIMES)

    def __init__(self, client=None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.name = self._settings.vision_pages_parser_name or "vision-pages"
        self.version = self._settings.vision_pages_parser_version or "0.1.0"
        self._client = client

    def can_handle(self, mime_type: str) -> bool:
        mime = (mime_type or "").split(";", 1)[0].strip().lower()
        if mime in self.supported_mime_types:
            return True
        # 扩展名兜底由 rasterize 判定；MIME 未知时仍尝试
        return mime in {"application/octet-stream", ""}

    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        settings = self._settings
        client = self._client or get_litellm_client()

        raster = render_document_to_page_images(
            file_path,
            filename=context.filename,
            mime_type=context.mime_type,
            settings=settings,
        )

        page_prompt_base = (settings.vision_pages_page_prompt or "").strip()
        model = (settings.vision_pages_model or settings.vision_model or "").strip()
        timeout = float(settings.vision_pages_timeout or settings.vision_timeout or 180)
        fail_fast = bool(settings.vision_pages_fail_fast)
        concurrency = int(getattr(settings, "vision_pages_concurrency", 1) or 1)
        if concurrency < 1:
            concurrency = 1

        page_results: list[tuple[int, str, list[ContentWarning], float | None]] = []
        if concurrency == 1 or len(raster.pages) <= 1:
            for page_img in raster.pages:
                page_results.append(
                    self._recognize_one_page(
                        client=client,
                        page_img=page_img,
                        file_path=file_path,
                        context=context,
                        page_prompt_base=page_prompt_base,
                        total_pages=raster.total_pages,
                        model=model,
                        timeout=timeout,
                        fail_fast=fail_fast,
                    )
                )
        else:
            workers = min(concurrency, len(raster.pages))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._recognize_one_page,
                        client=client,
                        page_img=page_img,
                        file_path=file_path,
                        context=context,
                        page_prompt_base=page_prompt_base,
                        total_pages=raster.total_pages,
                        model=model,
                        timeout=timeout,
                        fail_fast=fail_fast,
                    ): page_img.page
                    for page_img in raster.pages
                }
                # fail_fast：任一页抛错则取消其余（尽力）
                try:
                    for fut in as_completed(futures):
                        page_results.append(fut.result())
                except Exception:
                    for fut in futures:
                        fut.cancel()
                    raise

        # 按页号升序拼接（并发完成顺序不定）
        page_results.sort(key=lambda item: item[0])

        page_markdowns: list[tuple[int, str]] = []
        warnings: list[ContentWarning] = []
        confidences: list[float] = []
        for page_no, md, page_warnings, conf in page_results:
            page_markdowns.append((page_no, md))
            warnings.extend(page_warnings)
            if conf is not None:
                confidences.append(conf)

        if raster.truncated:
            warnings.append(
                ContentWarning(
                    type="parse_partial",
                    detail=(
                        f"vision-pages truncated to {len(raster.pages)} pages "
                        f"(total_pages={raster.total_pages}, "
                        f"max_pages={settings.vision_pages_max_pages})"
                    ),
                )
            )

        # 用 HTML page 注释分隔，便于 structure_blocks / 读路径按页切分
        parts: list[str] = []
        blocks: list[ContentBlock] = []
        for page_no, md in page_markdowns:
            parts.append(f"<!-- page {page_no} -->\n{md}".rstrip())
            blocks.append(
                ContentBlock(
                    id=f"page-{page_no:03d}",
                    type="paragraph",
                    page=page_no,
                    text=md,
                    confidence=None,
                    metadata={
                        "format": "markdown",
                        "kind": "vision_page",
                        "page": page_no,
                    },
                )
            )

        full_markdown = "\n\n".join(parts).strip() + ("\n" if parts else "")
        avg_conf = sum(confidences) / len(confidences) if confidences else None

        if not full_markdown.strip():
            warnings.append(
                ContentWarning(
                    type="parse_partial",
                    detail="vision-pages produced empty markdown",
                )
            )

        return StructuredContent(
            content_hash=context.content_hash,
            source=ContentSource(
                parser_name=self.name,
                parser_version=self.version,
                profile=self.profile,
            ),
            document=DocumentInfo(page_count=raster.total_pages),
            blocks=blocks,
            tables=[],
            warnings=warnings,
            metadata={
                "format": "markdown",
                "markdown": full_markdown,
                "char_count": len(full_markdown),
                "block_count": len(blocks),
                "page_count_rendered": len(raster.pages),
                "page_count_total": raster.total_pages,
                "truncated": raster.truncated,
                "via_libreoffice": raster.via_libreoffice,
                "model": model,
                "concurrency": concurrency,
                "filename": context.filename,
                "mime_type": context.mime_type,
                "media_kind": "document_pages",
                "confidence": avg_conf,
            },
        )

    def _recognize_one_page(
        self,
        *,
        client: Any,
        page_img: PageImage,
        file_path: str,
        context: ParseContext,
        page_prompt_base: str,
        total_pages: int,
        model: str,
        timeout: float,
        fail_fast: bool,
    ) -> tuple[int, str, list[ContentWarning], float | None]:
        """识别单页。返回 (page_no, markdown, warnings, confidence)。"""
        page_no = page_img.page
        warnings: list[ContentWarning] = []
        prompt = (
            f"{page_prompt_base}\n\n"
            f"（文件名：{context.filename or Path(file_path).name}；"
            f"第 {page_no} 页 / 共约 {total_pages} 页）"
        )
        try:
            result = client.analyze_image(
                page_img.png_bytes,
                mime_type=page_img.mime_type or "image/png",
                filename=f"{context.filename or 'doc'}_p{page_no}.png",
                prompt=prompt,
                model=model,
                timeout=timeout,
            )
            md = (result.markdown or "").strip()
            conf = float(result.confidence) if result.confidence is not None else None
            if not md:
                md = f"*(第 {page_no} 页：模型未返回可见文本)*"
                warnings.append(
                    ContentWarning(
                        block_id=f"page-{page_no:03d}",
                        type="parse_partial",
                        detail=f"vision-pages page {page_no} returned empty markdown",
                    )
                )
            return page_no, md, warnings, conf
        except Exception as exc:
            logger.warning("vision-pages page %s failed: %s", page_no, exc, exc_info=False)
            if fail_fast:
                raise
            err_text = f"*(第 {page_no} 页识别失败：{exc})*"
            warnings.append(
                ContentWarning(
                    block_id=f"page-{page_no:03d}",
                    type="parse_partial",
                    detail=f"vision-pages page {page_no} failed: {exc}",
                )
            )
            return page_no, err_text, warnings, None
