"""Deterministic redaction of sensitive identifiers, run before any LLM call or storage.

The detectors and their configuration live in hub.guard; this module keeps the old entry point.
"""
from __future__ import annotations

from .. import guard


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted text, counts per kind). Spans are never returned."""
    return guard.redact(text)
