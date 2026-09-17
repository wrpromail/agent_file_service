"""MinerU layout 解析器：调用 MinerU API 并映射为 StructuredContent。"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.config import get_settings
from app.integrations.mineru.client import MinerUClient, get_mineru_client
from app.integrations.mineru.mapper import map_content_list_to_structured
from app.integrations.mineru.zip_utils import extract_zip_full
from app.parsers.base import ParseContext, Parser
from app.schemas.content import StructuredContent


class MinerULayoutParser(Parser):
    name = "mineru"
    version = "0.1.0"
    profile = "layout"
    supported_mime_types = [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ]

    def __init__(self, client: MinerUClient | None = None) -> None:
        self._client = client

    def can_handle(self, mime_type: str) -> bool:
        return mime_type in self.supported_mime_types

    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        client = self._client or get_mineru_client()
        settings = get_settings()
        source = Path(file_path)
        work_root = Path(settings.mineru_work_dir)
        work_root.mkdir(parents=True, exist_ok=True)

        doc_dir = work_root / context.content_hash[:16]
        doc_dir.mkdir(parents=True, exist_ok=True)
        persistent_zip = doc_dir / "result.zip"

        with tempfile.TemporaryDirectory(prefix="mineru-parse-", dir=work_root) as temp_dir:
            batch = client.run_batch(
                source,
                doc_id=context.content_hash[:16],
                output_root=Path(temp_dir),
                poll_interval=settings.mineru_poll_interval,
                timeout=settings.mineru_timeout,
            )
            if not batch.records:
                raise RuntimeError("MinerU returned no records")

            record = batch.records[0]
            if record.get("state") != "done":
                raise RuntimeError(record.get("err_msg") or f"MinerU state={record.get('state')}")

            zip_path = record.get("downloaded_zip_path")
            if not zip_path:
                raise RuntimeError("MinerU completed without downloadable zip")
            shutil.copy2(zip_path, persistent_zip)

        parsed_dir = doc_dir / "parsed"
        if parsed_dir.exists():
            shutil.rmtree(parsed_dir)
        extract_zip_full(persistent_zip, parsed_dir)

        return map_content_list_to_structured(
            content_hash=context.content_hash,
            parsed_dir=parsed_dir,
            parser_name=self.name,
            parser_version=self.version,
            profile=self.profile,
        )