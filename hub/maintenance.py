"""Mirror rebuild, export archive and inbox import."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from . import entities, mirror, notes, pending
from .core import Hub
from .errors import ApiError

log = logging.getLogger(__name__)
SUFFIXES = (".md", ".txt")


def _all_notes(conn: sqlite3.Connection) -> list[dict]:
    return [notes.to_dict(conn, r) for r in conn.execute("SELECT * FROM notes ORDER BY id")]


def rebuild_mirror(hub: Hub, conn: sqlite3.Connection) -> dict:
    mirror.clear(hub.settings.data_dir)
    items = _all_notes(conn)
    for n in items:
        mirror.sync(hub.settings.data_dir, n, True)
    return {"ok": True, "notes": len(items)}


def export_zip(hub: Hub, conn: sqlite3.Connection):
    """Zip of notes (rendered from the DB), entities.jsonl and entity_types.json."""
    tmp = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)  # noqa: SIM115
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in _all_notes(conn):
            rel, text = mirror.render(n)
            z.writestr(f"notes/{rel}", text)
        lines = [json.dumps(entities.to_dict(conn, r), ensure_ascii=False)
                 for r in conn.execute("SELECT * FROM entities ORDER BY id")]
        z.writestr("entities.jsonl", "\n".join(lines) + ("\n" if lines else ""))
        z.writestr("entity_types.json",
                   json.dumps(entities.list_types(conn), ensure_ascii=False, indent=2))
    tmp.seek(0)
    return tmp


def _front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), text[m.end():]


def _parse(path: Path, text: str) -> dict:
    fm, body = _front_matter(text)
    body = body.strip()
    title = fm.get("title") if isinstance(fm.get("title"), str) else None
    if not title:
        h = re.search(r"^#{1,6}\s+(.+)$", body, re.MULTILINE)
        title = h.group(1).strip() if h else path.stem
    tags = fm.get("tags")
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    if not isinstance(tags, list):
        tags = []
    happened = fm.get("happened_at")
    if isinstance(happened, date):
        happened = happened.isoformat()
    data = {"content": body, "title": title, "tags": [str(t) for t in tags], "source": "import"}
    if isinstance(happened, str):
        data["happened_at"] = happened
    return data


def import_inbox(hub: Hub, conn: sqlite3.Connection, preprocess: bool = False,
                 actor: str = "ui") -> dict:
    if preprocess:
        pending.require_llm(hub)
    inbox = Path(hub.settings.data_dir) / "inbox"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out: dict = {"created": 0, "queued": 0, "skipped": 0, "errors": []}
    files = sorted(
        p for p in inbox.rglob("*")
        if p.suffix.lower() in SUFFIXES and p.is_file() and not p.is_symlink()
        and not any(part.startswith(".") or part == "_processed"
                    for part in p.relative_to(inbox).parts)
    )
    for p in files:
        rel = p.relative_to(inbox)
        try:
            data = _parse(p, p.read_text(encoding="utf-8"))
            if not data["content"]:
                out["skipped"] += 1
                continue
            h = hashlib.sha256(data["content"].encode()).hexdigest()
            if conn.execute("SELECT 1 FROM notes WHERE content_hash=? AND status='current'",
                            (h,)).fetchone():
                out["skipped"] += 1
                continue
            if preprocess:
                pending.create(hub, conn, data, actor, queue=True)
                out["queued"] += 1
            else:
                notes.create_note(hub, conn, data, actor)
                out["created"] += 1
        except ApiError as e:
            if e.code == "needs_reindex":
                raise
            out["errors"].append({"file": rel.as_posix(), "error": e.code})
            continue
        except (OSError, UnicodeDecodeError) as e:
            out["errors"].append({"file": rel.as_posix(), "error": type(e).__name__})
            continue
        dest = inbox / "_processed" / stamp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
    return out
