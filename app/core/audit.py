"""Structured audit logging for file-service mutations and sensitive reads."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.audit")


def audit_log(
    action: str,
    *,
    tenant_id: str | None = None,
    key_id: str | None = None,
    reference_id: str | None = None,
    **extra: Any,
) -> None:
    """Emit a single structured audit line (no secrets)."""
    parts = [
        "event=file_audit",
        f"action={action}",
    ]
    if tenant_id is not None:
        parts.append(f"tenant_id={tenant_id}")
    if key_id is not None:
        parts.append(f"key_id={key_id}")
    if reference_id is not None:
        parts.append(f"reference_id={reference_id}")
    for key, value in sorted(extra.items()):
        if value is None:
            continue
        parts.append(f"{key}={value}")
    logger.info(" ".join(parts))
