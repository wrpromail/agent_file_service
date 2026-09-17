from datetime import datetime, timezone


def parse_expires_at(raw: str | None) -> datetime | None:
    """Parse ISO 8601 expires_at; must be timezone-aware or treated as UTC."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO 8601 datetime") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    if dt <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")

    return dt