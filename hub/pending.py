"""AI tidy-up queue: pending notes awaiting LLM processing and human approval."""
from __future__ import annotations

import difflib
import json
import logging
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from . import llm, notes
from .core import Hub
from .db import tx
from .errors import ApiError, not_found, validation

log = logging.getLogger(__name__)

STATES = ("queued", "processing", "ready", "failed", "approved", "rejected")
EDITABLE = ("ready", "failed")
PURGE_DAYS = 30


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def llm_configured(hub: Hub) -> bool:
    return bool(hub.settings.llm_model)


def require_llm(hub: Hub) -> None:
    if not llm_configured(hub):
        raise ApiError("llm_not_configured", "AI tidy-up is not available", 409)


def _row(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM pending_notes WHERE uid=?", (uid,)).fetchone()
    if r is None:
        raise not_found("pending note not found")
    return r


def to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["uid"],
        "state": row["state"],
        "raw_content": row["raw_content"],
        "suggestion": {
            "title": row["title"], "content": row["content"],
            "tags": json.loads(row["tags"]), "happened_at": row["happened_at"],
        },
        "warnings": json.loads(row["warnings"]),
        "supersedes": json.loads(row["supersedes"]),
        "source": row["source"],
        "error": row["error"],
        "llm_model": row["llm_model"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def create(hub: Hub, conn: sqlite3.Connection, data: dict, actor: str, *, queue: bool) -> dict:
    """Insert a pending row. queue=True -> LLM job; False -> straight to review."""
    raw = notes._check_content(data.get("content"))
    tags = notes._clean_tags(data.get("tags"))
    happened = notes._clean_date(data.get("happened_at"))
    title = notes._check_title(data.get("title"), raw)
    source = actor if actor.startswith("agent:") else str(data.get("source") or "ui")
    uid = uuid.uuid4().hex
    with tx(conn):
        conn.execute(
            "INSERT INTO pending_notes(uid,raw_content,title,content,tags,happened_at,source,state) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uid, raw, title, raw, json.dumps(tags), happened, source,
             "queued" if queue else "ready"),
        )
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid) VALUES(?,?,?,?)",
            (actor, "pending_create", "pending", uid),
        )
    if queue:
        hub.queue.submit(uid)
    return to_dict(_row(conn, uid))


def list_pending(conn: sqlite3.Connection, state: str | None, limit: int, offset: int) -> dict:
    where, params = "", []
    if state:
        if state not in STATES:
            raise validation("invalid state")
        where, params = " WHERE state=?", [state]
    total = conn.execute(f"SELECT count(*) FROM pending_notes{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM pending_notes{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset]).fetchall()
    return {"items": [to_dict(r) for r in rows], "total": total}


def detail(hub: Hub, conn: sqlite3.Connection, uid: str) -> dict:
    from . import search

    d = to_dict(_row(conn, uid))
    sug = d["suggestion"]
    d["diff"] = "".join(difflib.unified_diff(
        d["raw_content"].splitlines(keepends=True), (sug["content"] or "").splitlines(keepends=True),
        "raw", "suggestion"))
    related: list[dict] = []
    if sug["content"] and d["state"] in ("ready", "failed"):
        q = f"{sug['title'] or ''}\n{sug['content']}"[:2000]
        for h in search.search(hub, conn, q, limit=3, kinds=["note"])["hits"]:
            related.append({k: h[k] for k in ("id", "title", "snippet", "similarity", "score")})
    d["related"] = related
    return d


def patch(conn: sqlite3.Connection, uid: str, data: dict) -> dict:
    row = _row(conn, uid)
    if row["state"] not in EDITABLE:
        raise ApiError("conflict", f"cannot edit a {row['state']} item", 409)
    title, content = row["title"], row["content"]
    tags, happened = json.loads(row["tags"]), row["happened_at"]
    if "content" in data:
        content = notes._check_content(data["content"])
    if "title" in data:
        title = notes._check_title(data["title"], content)
    if "tags" in data:
        tags = notes._clean_tags(data["tags"])
    if "happened_at" in data:
        happened = notes._clean_date(data["happened_at"])
    sup = json.loads(row["supersedes"])
    if "supersedes" in data:
        sup = _uid_list(data["supersedes"])
    with tx(conn):
        conn.execute(
            "UPDATE pending_notes SET title=?, content=?, tags=?, happened_at=?, supersedes=? "
            "WHERE id=?", (title, content, json.dumps(tags), happened, json.dumps(sup), row["id"]))
    return to_dict(_row(conn, uid))


def _uid_list(v: object) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise validation("supersede must be a list of note ids")
    return list(dict.fromkeys(v))


def approve(hub: Hub, conn: sqlite3.Connection, uid: str, data: dict, actor: str) -> dict:
    row = _row(conn, uid)
    if row["state"] not in EDITABLE:
        raise ApiError("conflict", f"cannot approve a {row['state']} item", 409)
    body = {
        "title": data.get("title", row["title"]),
        "content": data.get("content", row["content"]),
        "tags": data.get("tags", json.loads(row["tags"])),
        "happened_at": data.get("happened_at", row["happened_at"]),
        "source": row["source"],
    }
    sup = _uid_list(data["supersede"]) if "supersede" in data else json.loads(row["supersedes"])
    raw = row["raw_content"] if body["content"] != row["raw_content"] else None

    def mark(c: sqlite3.Connection) -> None:
        n = c.execute(
            "UPDATE pending_notes SET state='approved', decided_at=? WHERE id=? "
            "AND state IN ('ready','failed')", (_now(), row["id"])).rowcount
        if n != 1:
            raise ApiError("conflict", "item was already decided", 409)

    note = notes.create_note(hub, conn, body, actor, raw_content=raw, supersede=sup, in_tx=mark)
    return {"note": note, "pending": to_dict(_row(conn, uid))}


def reject(conn: sqlite3.Connection, uid: str, actor: str) -> dict:
    row = _row(conn, uid)
    if row["state"] not in EDITABLE:
        raise ApiError("conflict", f"cannot reject a {row['state']} item", 409)
    with tx(conn):
        conn.execute("UPDATE pending_notes SET state='rejected', decided_at=? WHERE id=?",
                     (_now(), row["id"]))
        conn.execute("INSERT INTO audit_log(actor,action,target_kind,target_uid) VALUES(?,?,?,?)",
                     (actor, "pending_reject", "pending", uid))
    return to_dict(_row(conn, uid))


def regenerate(hub: Hub, conn: sqlite3.Connection, uid: str) -> dict:
    require_llm(hub)
    row = _row(conn, uid)
    if row["state"] not in EDITABLE:
        raise ApiError("conflict", f"cannot regenerate a {row['state']} item", 409)
    with tx(conn):
        conn.execute(
            "UPDATE pending_notes SET state='queued', content=raw_content, error=NULL, "
            "warnings='{}' WHERE id=?", (row["id"],))
    hub.queue.submit(uid)
    return to_dict(_row(conn, uid))


class Queue:
    """Thread pool running LLM jobs. Never holds a DB transaction while calling the LLM."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.pool = ThreadPoolExecutor(max_workers=hub.settings.llm_concurrency,
                                       thread_name_prefix="llm")

    def submit(self, uid: str) -> None:
        self.pool.submit(self._run, uid)

    def recover(self) -> None:
        """Startup: purge old rejected rows; requeue unfinished jobs."""
        cutoff = (datetime.now(UTC) - timedelta(days=PURGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self.hub.conn()
        try:
            conn.execute("DELETE FROM pending_notes WHERE state='rejected' AND decided_at < ?",
                         (cutoff,))
            if not llm_configured(self.hub):
                return
            conn.execute("UPDATE pending_notes SET state='queued' WHERE state='processing'")
            uids = [r["uid"] for r in conn.execute(
                "SELECT uid FROM pending_notes WHERE state='queued' ORDER BY id")]
        finally:
            conn.close()
        for u in uids:
            self.submit(u)

    def _run(self, uid: str) -> None:
        try:
            self._job(uid)
        except Exception as e:  # noqa: BLE001 - a job must never kill the worker
            log.error("pending job failed: %s", type(e).__name__)

    def _job(self, uid: str) -> None:
        hub = self.hub
        conn = hub.conn()
        try:
            n = conn.execute(
                "UPDATE pending_notes SET state='processing' WHERE uid=? AND state='queued'",
                (uid,)).rowcount
            if n != 1:
                return
            raw = _row(conn, uid)["raw_content"]
            sug, err = None, None
            if len(raw) > hub.settings.llm_max_input_chars:
                err = "too_long"
            else:
                try:
                    sug = llm.tidy(hub.settings, raw)
                except llm.LLMError as e:
                    err = str(e)
            if sug is None:
                conn.execute(
                    "UPDATE pending_notes SET state='failed', error=?, llm_model=? "
                    "WHERE uid=? AND state='processing'",
                    (err, hub.settings.llm_model, uid))
                return
            warnings = {"missing": llm.missing_facts(raw, f"{sug.title}\n{sug.content}")}
            conn.execute(
                "UPDATE pending_notes SET state='ready', error=NULL, title=?, content=?, tags=?, "
                "happened_at=?, warnings=?, llm_model=? WHERE uid=? AND state='processing'",
                (sug.title[:200] or notes.default_title(sug.content), sug.content,
                 json.dumps(sug.tags), sug.happened_at, json.dumps(warnings),
                 hub.settings.llm_model, uid))
        finally:
            conn.close()
