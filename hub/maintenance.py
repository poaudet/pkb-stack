"""Phase 4 extras: mirror rebuild, export, inbox import, reindex job."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import entities, mirror, notes, pending
from .core import Hub
from .db import set_meta, tx
from .errors import ApiError, validation

log = logging.getLogger(__name__)

IMPORT_SUFFIXES = (".md", ".txt")


# ---- mirror rebuild / export

def rebuild_mirror(hub: Hub, conn) -> dict:
    root = Path(hub.settings.data_dir) / "notes"
    if root.is_dir():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in conn.execute("SELECT * FROM notes ORDER BY id").fetchall():
        mirror.sync(hub.settings.data_dir, notes.to_dict(conn, r), True)
        n += 1
    return {"notes": n}


def build_export(hub: Hub, conn) -> bytes:
    buf = io.BytesIO()
    root = Path(hub.settings.data_dir) / "notes"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if root.is_dir():
            for p in sorted(root.rglob("*.md")):
                z.write(p, "notes/" + p.relative_to(root).as_posix())
        lines = [json.dumps(entities.to_dict(conn, r), ensure_ascii=False)
                 for r in conn.execute("SELECT * FROM entities ORDER BY id").fetchall()]
        z.writestr("entities.jsonl", "\n".join(lines) + ("\n" if lines else ""))
        z.writestr("entity_types.json",
                   json.dumps(entities.list_types(conn), ensure_ascii=False, indent=2))
    return buf.getvalue()


# ---- inbox import

def _parse_file(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta: dict = {}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                meta = loaded
        except yaml.YAMLError:
            meta = {}
        text = text[m.end():]
    return meta, text.strip()


def _title_for(meta: dict, body: str, path: Path) -> str:
    if isinstance(meta.get("title"), str) and meta["title"].strip():
        return meta["title"].strip()[:200]
    for line in body.splitlines():
        if line.startswith("#"):
            t = line.lstrip("#").strip()
            if t:
                return t[:200]
    return path.stem.replace("_", " ").replace("-", " ").strip()[:200] or "Untitled"


def _iter_inbox(inbox: Path):
    for p in sorted(inbox.rglob("*")):
        rel = p.relative_to(inbox).parts
        if not p.is_file() or p.suffix.lower() not in IMPORT_SUFFIXES:
            continue
        if any(part.startswith(".") or part == "_processed" for part in rel):
            continue
        yield p


def scan_inbox(hub: Hub, conn, preprocess: bool = False, actor: str = "ui") -> dict:
    if preprocess:
        pending.require_llm(hub)
    inbox = Path(hub.settings.data_dir) / "inbox"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out: dict = {"imported": 0, "skipped": 0, "errors": 0, "files": []}
    for p in _iter_inbox(inbox):
        rel = p.relative_to(inbox).as_posix()
        status = "imported"
        try:
            meta, body = _parse_file(p)
            if not body:
                status = "skipped"
            else:
                h = hashlib.sha256(body.encode()).hexdigest()
                dup = conn.execute(
                    "SELECT 1 FROM notes WHERE content_hash=? AND status='current'", (h,)
                ).fetchone()
                if dup:
                    status = "skipped"
                else:
                    data = {"content": body, "title": _title_for(meta, body, p),
                            "source": "import"}
                    tags = meta.get("tags")
                    if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                        data["tags"] = tags
                    if preprocess:
                        pending.create(hub, conn, data, actor, queue=True)
                    else:
                        notes.create_note(hub, conn, data, actor)
            dest = inbox / "_processed" / stamp / p.relative_to(inbox)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
        except (ApiError, OSError) as e:
            status = "error"
            log.warning("import of %s failed: %s", rel, getattr(e, "message", type(e).__name__))
        out["files"].append({"file": rel, "status": status})
        key = {"imported": "imported", "skipped": "skipped", "error": "errors"}[status]
        out[key] += 1
    return out


# ---- reindex job

class ReindexJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = {"state": "idle", "done": 0, "total": 0, "error": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def start(self, hub: Hub, wait: bool = False) -> dict:
        with self._lock:
            if self.state["state"] == "running":
                raise ApiError("conflict", "reindex already running", 409)
            self.state = {"state": "running", "done": 0, "total": 0, "error": None}
        t = threading.Thread(target=self._run, args=(hub,), daemon=True)
        t.start()
        if wait:
            t.join()
        return self.status()

    def _run(self, hub: Hub) -> None:
        conn = hub.conn()
        try:
            reindex(hub, conn, progress=lambda d, t: self._set(done=d, total=t))
            self._set(state="done")
        except Exception as e:  # noqa: BLE001
            log.error("reindex failed: %s", type(e).__name__)
            self._set(state="error", error=type(e).__name__)
        finally:
            conn.close()


def reindex(hub: Hub, conn, progress=None) -> dict:
    """Re-chunk and re-embed every note and embeddable entity with the active embedder."""
    emb = hub.embedder
    items: list[tuple[str, int, str, list[str]]] = []
    for r in conn.execute("SELECT id, title, content FROM notes ORDER BY id").fetchall():
        from .chunking import chunk_markdown

        items.append(("note", r["id"], r["title"],
                      chunk_markdown(r["content"], emb.profile.chunk_chars)))
    for r in conn.execute(
        "SELECT e.id, e.type, e.data, t.text_template FROM entities e "
        "JOIN entity_types t ON t.name=e.type WHERE t.embed=1 ORDER BY e.id"
    ).fetchall():
        data = json.loads(r["data"])
        items.append(("entity", r["id"], entities.entity_title(r["type"], data),
                      [entities.render_text(r["type"], r["text_template"], data)]))
    total = len(items)
    prepared = []
    for i, (kind, oid, title, chunks) in enumerate(items, 1):
        vecs = emb.embed_documents([f"{title}\n{c}" for c in chunks])
        prepared.append((kind, oid, title, chunks, vecs))
        if progress:
            progress(i, total)
    with tx(conn):
        conn.execute("DELETE FROM chunks")
        hub.store.reset(conn)
        hub.store.ensure(conn, emb.dim)
        for kind, oid, title, chunks, vecs in prepared:
            notes._write_chunks(hub, conn, oid, title, chunks, vecs, kind=kind)
        set_meta(conn, "embed_model", emb.model)
        set_meta(conn, "embed_dim", str(emb.dim))
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,detail) VALUES(?,?,?,?)",
            ("ui", "reindex", "system", f"{total} items"),
        )
    hub.needs_reindex = False
    return {"items": total}


def require_dict(v: object) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise validation("body must be a JSON object")
    return v
