"""Deterministic text splitting for long capture (no LLM)."""
from __future__ import annotations

import re
from itertools import pairwise

_BLANK = re.compile(r"\n[ \t]*\n\s*")
_SENTENCE = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_LINE = re.compile(r"[^\n]*\n|[^\n]+$")


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return False
    return stripped.startswith("#") or stripped[-1] not in ".!?,;"


def _boundaries(text: str) -> list[int]:
    cuts = {0, len(text)}
    for match in _BLANK.finditer(text):
        cuts.add(match.end())
    for match in _LINE.finditer(text):
        line = match.group(0)
        if line.endswith("\n") and _is_heading(line):
            cuts.add(match.start())
            cuts.add(match.end())
    for match in _SENTENCE.finditer(text):
        cuts.add(match.end())
    return sorted(cuts)


def split_text(text: str, target: int) -> list[dict]:
    """Return contiguous chunks [{idx,start_char,end_char}] covering the whole text."""
    if not text:
        return []
    target = max(1, target)
    cuts = _boundaries(text)
    pieces = list(pairwise(cuts))
    chunks: list[tuple[int, int]] = []
    start = end = None
    for lo, hi in pieces:
        if start is None:
            start, end = lo, hi
        elif hi - start <= target:
            end = hi
        else:
            chunks.append((start, end))
            start, end = lo, hi
    if start is not None:
        chunks.append((start, end))
    return [{"idx": i, "start_char": lo, "end_char": hi} for i, (lo, hi) in enumerate(chunks)]


def overlap_context(text: str, chunks: list[dict], idx: int, overlap_chars: int) -> str:
    """Tail of the previous chunk's last paragraph, as context only."""
    if idx <= 0 or overlap_chars <= 0:
        return ""
    prev = chunks[idx - 1]
    body = text[prev["start_char"]:prev["end_char"]].rstrip()
    paragraph = _BLANK.split(body)[-1].strip()
    if len(paragraph) <= overlap_chars:
        return paragraph
    tail = paragraph[-overlap_chars:]
    cut = re.search(r"\s", tail)
    return tail[cut.end():] if cut else tail
