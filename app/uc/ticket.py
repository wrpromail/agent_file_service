"""本地 UC 会话票（HMAC）。格式：

``v1.{userId}.{jti}.{exp}.{sig}``

HMAC-SHA256 签 ``userId:jti:exp``；TTL 默认 8h，无滑动续期。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

HEADER_UC_SESSION = "x-uc-session"
DEFAULT_TTL_SEC = 28800
JTI_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class ParsedTicket:
    user_id: str
    jti: str
    exp: int
    sig: str


def parse_ticket(ticket: str | None) -> ParsedTicket | None:
    raw = str(ticket or "").strip()
    if not raw.startswith("v1."):
        return None
    parts = raw.split(".")
    if len(parts) < 5:
        return None
    sig = parts[-1]
    exp_s = parts[-2]
    jti = parts[-3]
    uid = ".".join(parts[1:-3])
    if not uid or not exp_s.isdigit() or not JTI_RE.match(jti) or not sig:
        return None
    return ParsedTicket(user_id=uid, jti=jti, exp=int(exp_s), sig=sig)


def issue_ticket(user_id: str, *, secret: str | None = None, ttl_sec: int | None = None) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id required")
    key = str(secret or "uc-dev")
    ttl = max(60, int(ttl_sec or DEFAULT_TTL_SEC))
    jti = secrets.token_hex(16)
    exp = int(time.time()) + ttl
    msg = f"{uid}:{jti}:{exp}"
    sig = hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1.{uid}.{jti}.{exp}.{sig}"


def verify_ticket(ticket: str | None, *, secret: str | None = None) -> str | None:
    parsed = parse_ticket(ticket)
    if parsed is None:
        return None
    if parsed.exp < int(time.time()):
        return None
    key = str(secret or "uc-dev")
    msg = f"{parsed.user_id}:{parsed.jti}:{parsed.exp}"
    expect = hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    if len(parsed.sig) != len(expect):
        return None
    if not hmac.compare_digest(parsed.sig, expect):
        return None
    return parsed.user_id
