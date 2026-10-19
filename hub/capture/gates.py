"""Pre-LLM safety gates and daily usage accounting."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..db import tx
from ..errors import ApiError

SECRET_PATTERNS = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.")),
    ("credential", re.compile(
        r"(?i)(password|mot de passe|passwd|api[_ -]?key|token|secret)\s*[:=]\s*\S{6,}"
    )),
)


def normalized_text(text: str) -> str:
    return " ".join(text.split())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalized_text(text).encode("utf-8")).hexdigest()


def _luhn(value: str) -> bool:
    digits = [int(c) for c in value]
    total = 0
    parity = len(digits) % 2
    for i, digit in enumerate(digits):
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def secret_kind(text: str) -> str | None:
    for kind, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return kind
    for match in re.finditer(r"(?<!\d)\d{13,19}(?!\d)", text):
        if _luhn(match.group()):
            return "payment_card"
    return None


def current_day() -> str:
    return datetime.now(UTC).date().isoformat()


def resets_at() -> str:
    next_day = datetime.now(UTC).date() + timedelta(days=1)
    return next_day.isoformat() + "T00:00:00Z"


def usage(conn) -> int:
    row = conn.execute("SELECT calls FROM capture_usage WHERE day=?", (current_day(),)).fetchone()
    return int(row["calls"]) if row else 0


def check_limit(conn, settings: Settings) -> None:
    limit = settings.capture_daily_call_limit
    calls = usage(conn)
    if limit > 0 and calls >= limit:
        raise ApiError("rate_limited", "daily capture LLM call limit reached", 429,
                       resets_at=resets_at())


def consume_call(conn, settings: Settings) -> None:
    """Atomically reserve one call; concurrent jobs cannot exceed the daily limit."""
    day, limit = current_day(), settings.capture_daily_call_limit
    with tx(conn):
        row = conn.execute("SELECT calls FROM capture_usage WHERE day=?", (day,)).fetchone()
        calls = int(row["calls"]) if row else 0
        if limit > 0 and calls >= limit:
            raise ApiError("rate_limited", "daily capture LLM call limit reached", 429,
                           resets_at=resets_at())
        conn.execute(
            "INSERT INTO capture_usage(day,calls) VALUES(?,1) "
            "ON CONFLICT(day) DO UPDATE SET calls=calls+1",
            (day,),
        )


def gate(conn, settings: Settings, text: str, *, allow_secrets: bool = False,
         allow_duplicate: bool = False, max_chars: int | None = None) -> str:
    if not isinstance(text, str):
        raise ApiError("validation_error", "text must be a string", 400)
    if len(text) > (max_chars or settings.capture_max_chars):
        raise ApiError("too_long", "text exceeds CAPTURE_MAX_CHARS; save it as a note", 400)
    if settings.capture_block_secrets and not allow_secrets:
        kind = secret_kind(text)
        if kind:
            raise ApiError("secret_detected", f"possible {kind} detected", 409, kind=kind)
    digest = text_hash(text)
    if not allow_duplicate:
        cutoff = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = conn.execute(
            "SELECT uid FROM captures WHERE text_hash=? AND state='applied' AND created_at>=? "
            "ORDER BY id DESC LIMIT 1",
            (digest, cutoff),
        ).fetchone()
        if row:
            raise ApiError("duplicate", "this text was applied recently", 409,
                           capture_id=row["uid"])
    check_limit(conn, settings)
    return digest
