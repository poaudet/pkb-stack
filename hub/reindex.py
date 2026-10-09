"""Background re-embedding of every note and embeddable entity."""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime

from . import db, entities, notes
from .chunking import chunk_markdown
from .core import Hub
from .db import tx
from .errors import ApiError

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Reindexer:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._lock = threading.Lock()
        self._status: dict = {"state": "idle"}

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def start(self, actor: str = "ui", *, wait: bool = False) -> dict:
        with self._lock:
            if self._status["state"] == "running":
                raise ApiError("conflict", "a reindex is already running", 409)
            self._status = {"state": "running", "done": 0, "total": 0, "started_at": _now()}
        t = threading.Thread(target=self._run, args=(actor,), name="reindex", daemon=True)
        t.start()
        if wait:
            t.join()
        return self.status()

    def _set(self, **kw: object) -> None:
        with self._lock:
            self._status.update(kw)

    def _run(self, actor: str) -> None:
        hub = self.hub
        conn = hub.conn()
        try:
            if hub.needs_reindex:
                with tx(conn):
                    hub.store.reset(conn, hub.embedder.dim)
                    db.set_meta(conn, "embed_dim", str(hub.embedder.dim))
            note_rows = conn.execute("SELECT id, title, content FROM notes").fetchall()
            types = {r["name"]: r for r in conn.execute("SELECT * FROM entity_types")}
            ents = [r for r in conn.execute("SELECT id, type, data FROM entities")
                    if r["type"] in types and types[r["type"]]["embed"]]
            self._set(total=len(note_rows) + len(ents))
            done = 0
            for r in note_rows:
                chunks = chunk_markdown(r["content"], hub.embedder.profile.chunk_chars)
                vecs = hub.embedder.embed_documents([f"{r['title']}\n{c}" for c in chunks])
                with tx(conn):
                    notes._write_chunks(hub, conn, r["id"], r["title"], chunks, vecs)
                done += 1
                self._set(done=done)
            for r in ents:
                data = json.loads(r["data"])
                t = types[r["type"]]
                title = entities.entity_title(t["name"], data)
                text = entities.render_text(
                    t["name"], t["text_template"], data, json.loads(t["json_schema"])
                )
                vecs = hub.embedder.embed_documents([f"{title}\n{text}"])
                with tx(conn):
                    notes._write_chunks(hub, conn, r["id"], title, [text], vecs, kind="entity")
                done += 1
                self._set(done=done)
            with tx(conn):
                db.set_meta(conn, "embed_model", hub.embedder.model)
                db.set_meta(conn, "embed_dim", str(hub.embedder.dim))
                conn.execute(
                    "INSERT INTO audit_log(actor,action,target_kind,target_uid) VALUES(?,?,?,?)",
                    (actor, "reindex", "system", None),
                )
            hub.needs_reindex = False
            self._set(state="done", finished_at=_now())
        except Exception as e:  # noqa: BLE001
            log.error("reindex failed: %s", type(e).__name__)
            self._set(state="failed", error=type(e).__name__, finished_at=_now())
        finally:
            conn.close()
