"""File Service SDK 响应模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContentResult:
    """``GET /v1/files/{id}/content`` 的封装（含 ETag / 304）。"""

    body: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None
    not_modified: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.body.get("ready"))

    @property
    def status(self) -> str:
        return str(self.body.get("status") or "")

    @property
    def message(self) -> str | None:
        msg = self.body.get("message")
        return str(msg) if msg is not None else None

    @property
    def message_code(self) -> str | None:
        code = self.body.get("message_code")
        return str(code) if code is not None else None

    @property
    def markdown(self) -> str | None:
        meta = self.body.get("metadata")
        if not isinstance(meta, dict):
            return None
        md = meta.get("markdown")
        return str(md) if md is not None else None

    @property
    def tables_json(self) -> Any:
        meta = self.body.get("metadata")
        if not isinstance(meta, dict):
            return None
        return meta.get("tables_json")
