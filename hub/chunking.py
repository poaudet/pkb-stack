"""Markdown chunking: headings/blank lines, merge to a size, ~150 chars overlap."""
from __future__ import annotations

import re

SINGLE_CHUNK_MAX = 1200
OVERLAP = 150
_SENT = re.compile(r"(?<=[.!?。])\s+")


def _split_long(par: str, size: int) -> list[str]:
    if len(par) <= size:
        return [par]
    out: list[str] = []
    cur = ""
    for s in _SENT.split(par):
        while len(s) > size:  # no sentence end: hard split on whitespace/size
            cut = s.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > size:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return [p for p in out if p]


def chunk_markdown(text: str, chunk_chars: int = 900) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) < SINGLE_CHUNK_MAX and len(text) <= max(chunk_chars, SINGLE_CHUNK_MAX):
        return [text]
    blocks: list[str] = []
    for part in re.split(r"\n\s*\n|\n(?=#{1,6}\s)", text):
        part = part.strip()
        if part:
            blocks.extend(_split_long(part, chunk_chars))
    chunks: list[str] = []
    cur = ""
    for b in blocks:
        if cur and len(cur) + 2 + len(b) > chunk_chars:
            chunks.append(cur)
            tail = cur[-OVERLAP:]
            sp = tail.find(" ")
            tail = tail[sp + 1 :] if sp != -1 else tail
            cur = f"{tail}\n\n{b}" if tail else b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    return chunks
