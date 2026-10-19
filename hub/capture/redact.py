"""Deterministic redaction of sensitive identifiers, run before any LLM call or storage."""
from __future__ import annotations

import re
from collections import Counter

_SEP = r"[ \-]?"
_HEALTH = re.compile(rf"\b[A-Z]{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}(?:{_SEP}\d{{1,4}})?\b")
_PAYMENT = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_SIN = re.compile(r"(?<![\d-])\d{3}[ -]\d{3}[ -]\d{3}(?![\d-])")
_PASSPORT = re.compile(
    r"(\bpassport(?:\s+(?:number|no\.?|#))?\s*[:#]?\s*)([A-Z]{1,2}\d{6,8}|[A-Z0-9]{8,9})\b",
    re.IGNORECASE,
)
_LICENCE = re.compile(
    r"(\b(?:driver'?s?|driving)\s+licen[cs]e(?:\s+(?:number|no\.?|#))?\s*[:#]?\s*(?:is\s+)?)"
    r"([A-Z0-9][A-Z0-9 \-]{5,19}[A-Z0-9])(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_BANK = re.compile(
    r"(\b(?:account|IBAN)(?:\s+(?:number|no\.?|#))?\s*[:#]?\s*(?:is\s+)?)"
    r"((?=[A-Z0-9 \-]*\d)[A-Z0-9][A-Z0-9 \-]{4,33}[A-Z0-9])(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted text, counts per kind). Spans are never returned."""
    counts: Counter[str] = Counter()

    def whole(kind: str):
        def sub(match: re.Match) -> str:
            counts[kind] += 1
            return _marker(kind)
        return sub

    def tail(kind: str):
        def sub(match: re.Match) -> str:
            counts[kind] += 1
            return match.group(1) + _marker(kind)
        return sub

    for pattern, kind, fn in (
        (_HEALTH, "health_card", whole),
        (_SIN, "government_id", whole),
        (_PASSPORT, "government_id", tail),
        (_LICENCE, "government_id", tail),
        (_BANK, "bank_account", tail),
        (_PAYMENT, "payment_card", whole),
    ):
        text = pattern.sub(fn(kind), text)
    return text, dict(counts)
