"""Markdown mirror of notes (best-effort; DB is the source of truth)."""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def _slug(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "note"


def _find(root: Path, uid: str) -> list[Path]:
    return [p for p in root.rglob(f"*-{uid[:8]}.md") if p.is_file()]


def remove(data_dir: str, uid: str) -> None:
    try:
        for p in _find(Path(data_dir) / "notes", uid):
            p.unlink()
    except OSError as e:
        log.warning("mirror remove failed: %s", e)


def render(note: dict) -> tuple[str, str]:
    """Return (path relative to the notes dir, markdown text) for a note dict."""
    date = (note.get("happened_at") or note["created_at"])[:10]
    year = date[:4] if re.match(r"\d{4}", date) else "undated"
    rel = Path("_history" if note["status"] != "current" else "") / year
    rel = rel / f"{date}-{_slug(note['title'])}-{note['id'][:8]}.md"
    fm = {
        "id": note["id"],
        "title": note["title"],
        "tags": note["tags"],
        "status": note["status"],
        "created": note["created_at"],
        "updated": note["updated_at"],
        "happened_at": note.get("happened_at"),
        "superseded_by": note.get("superseded_by"),
    }
    body = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n"
    return rel.as_posix(), body + note["content"] + "\n"


def sync(data_dir: str, note: dict, enabled: bool = True) -> None:
    if not enabled:
        return
    try:
        root = Path(data_dir) / "notes"
        remove(data_dir, note["id"])
        rel, text = render(note)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("mirror write failed: %s", e)


def clear(data_dir: str) -> None:
    """Delete every mirrored markdown file (and now-empty directories)."""
    root = Path(data_dir) / "notes"
    for p in root.rglob("*.md*"):
        if p.is_file():
            p.unlink()
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
