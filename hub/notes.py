"""Note lifecycle: create, patch, supersede, archive/restore, forget."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, date, datetime

from . import mirror
from .core import Hub
from .db import tx
from .errors import ApiError, not_found, validation

MAX_CONTENT = 200_000
STATUSES = ("current", "superseded", "archived")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_title(content: str) -> str:
    for line in content.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "Untitled"


def _clean_tags(tags: object) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise validation("tags must be a list of strings")
    out: list[str] = []
    for t in tags:
        t = t.strip().lower()
        if t and t not in out:
            out.append(t)
    return out[:50]


def _clean_date(v: object) -> str | None:
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise validation("happened_at must be YYYY-MM-DD")
    try:
        date.fromisoformat(v)
    except ValueError as e:
        raise validation("happened_at must be a valid date") from e
    return v


def _check_content(content: object) -> str:
    if not isinstance(content, str) or not content.strip():
        raise validation("content must be a non-empty string")
    if len(content) > MAX_CONTENT:
        raise ApiError("too_large", "note exceeds 200000 characters", 413)
    return content


def _check_title(title: object, content: str) -> str:
    if title is None or title == "":
        return default_title(content)
    if not isinstance(title, str):
        raise validation("title must be a string")
    return title.strip()[:200] or default_title(content)


def _uid_of(conn: sqlite3.Connection, note_id: int | None) -> str | None:
    if note_id is None:
        return None
    r = conn.execute("SELECT uid FROM notes WHERE id=?", (note_id,)).fetchone()
    return r["uid"] if r else None


def to_dict(conn: sqlite3.Connection, row: sqlite3.Row, full: bool = True) -> dict:
    d = {
        "id": row["uid"],
        "title": row["title"],
        "tags": json.loads(row["tags"]),
        "happened_at": row["happened_at"],
        "source": row["source"],
        "status": row["status"],
        "superseded_by": _uid_of(conn, row["superseded_by"]),
        "status_reason": row["status_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if full:
        d["content"] = row["content"]
        d["raw_content"] = row["raw_content"]
    return d


def _row(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM notes WHERE uid=?", (uid,)).fetchone()
    if r is None:
        raise not_found("note not found")
    return r


def _require_embeddings(hub: Hub) -> None:
    if hub.needs_reindex:
        raise ApiError("needs_reindex", "embedding model changed; reindex required", 409)


def _prepare(hub: Hub, title: str, content: str):
    from .chunking import chunk_markdown

    chunks = chunk_markdown(content, hub.embedder.profile.chunk_chars)
    vecs = hub.embedder.embed_documents([f"{title}\n{c}" for c in chunks])
    return chunks, vecs


def _write_chunks(hub: Hub, conn: sqlite3.Connection, note_id: int, title: str, chunks, vecs,
                  kind: str = "note"):
    for r in conn.execute(
        "SELECT id FROM chunks WHERE owner_kind=? AND owner_id=?", (kind, note_id)
    ).fetchall():
        hub.store.delete(conn, [r["id"]])
    conn.execute("DELETE FROM chunks WHERE owner_kind=? AND owner_id=?", (kind, note_id))
    for i, (text, vec) in enumerate(zip(chunks, vecs, strict=True)):
        cid = conn.execute(
            "INSERT INTO chunks(owner_kind, owner_id, idx, title, text) "
            "VALUES(?,?,?,?,?) RETURNING id",
            (kind, note_id, i, title, text),
        ).fetchone()["id"]
        hub.store.upsert(conn, cid, vec)


def _insert(hub, conn, *, title, content, tags, happened_at, source, raw=None, chunks, vecs):
    uid = uuid.uuid4().hex
    nid = conn.execute(
        "INSERT INTO notes(uid,title,content,raw_content,content_hash,tags,happened_at,source) "
        "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
        (uid, title, content, raw, hashlib.sha256(content.encode()).hexdigest(),
         json.dumps(tags), happened_at, source),
    ).fetchone()["id"]
    _write_chunks(hub, conn, nid, title, chunks, vecs)
    return nid, uid


def _audit(conn, actor: str, action: str, uid: str | None, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor,action,target_kind,target_uid,detail) VALUES(?,?,?,?,?)",
        (actor, action, "note", uid, detail),
    )


def _mirror(hub: Hub, conn: sqlite3.Connection, uid: str) -> None:
    mirror.sync(hub.settings.data_dir, to_dict(conn, _row(conn, uid)), hub.settings.mirror_markdown)


def create_note(hub: Hub, conn, data: dict, actor: str = "ui", *, raw_content: str | None = None,
                supersede: list[str] | None = None, in_tx=None) -> dict:
    _require_embeddings(hub)
    content = _check_content(data.get("content"))
    title = _check_title(data.get("title"), content)
    tags = _clean_tags(data.get("tags"))
    happened = _clean_date(data.get("happened_at"))
    h = hashlib.sha256(content.encode()).hexdigest()
    if not data.get("allow_duplicate"):
        dup = conn.execute(
            "SELECT uid FROM notes WHERE content_hash=? AND status='current'", (h,)
        ).fetchone()
        if dup:
            raise ApiError("duplicate", "an identical current note exists", 409, existing=dup["uid"])
    olds = [_row(conn, u) for u in (supersede or [])]
    if any(o["status"] != "current" for o in olds):
        raise ApiError("conflict", "only current notes can be superseded", 409)
    chunks, vecs = _prepare(hub, title, content)
    source = actor if actor.startswith("agent:") else str(data.get("source") or "ui")
    with tx(conn):
        nid, uid = _insert(hub, conn, title=title, content=content, tags=tags,
                           happened_at=happened, source=source, raw=raw_content,
                           chunks=chunks, vecs=vecs)
        for o in olds:
            conn.execute(
                "UPDATE notes SET status='superseded', superseded_by=?, status_reason=?, "
                "updated_at=? WHERE id=?",
                (nid, "replaced via review", _now(), o["id"]),
            )
            _audit(conn, actor, "supersede", o["uid"], uid)
        _audit(conn, actor, "create", uid)
        if in_tx is not None:
            in_tx(conn)
    for o in olds:
        _mirror(hub, conn, o["uid"])
    _mirror(hub, conn, uid)
    return to_dict(conn, _row(conn, uid))


def get_note(conn, uid: str) -> dict:
    return to_dict(conn, _row(conn, uid))


def list_notes(conn, status=None, tag=None, q=None, limit=50, offset=0) -> dict:
    where, params = [], []
    if status:
        if status not in STATUSES:
            raise validation("invalid status")
        where.append("status=?")
        params.append(status)
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(notes.tags) WHERE value=?)")
        params.append(tag.strip().lower())
    if q:
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(lower(title) LIKE ? ESCAPE '\\' OR lower(content) LIKE ? ESCAPE '\\')")
        params += [like, like]
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT count(*) FROM notes{w}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM notes{w} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {"items": [to_dict(conn, r, full=False) for r in rows], "total": total}


def patch_note(hub: Hub, conn, uid: str, data: dict, actor: str = "ui") -> dict:
    row = _row(conn, uid)
    title, content = row["title"], row["content"]
    tags, happened = json.loads(row["tags"]), row["happened_at"]
    rechunk = False
    if "content" in data:
        content = _check_content(data["content"])
        rechunk = True
    if "title" in data:
        title = _check_title(data["title"], content)
        rechunk = True
    elif rechunk and row["title"] == default_title(row["content"]):
        title = default_title(content)
    if "tags" in data:
        tags = _clean_tags(data["tags"])
    if "happened_at" in data:
        happened = _clean_date(data["happened_at"])
    chunks = vecs = None
    if rechunk:
        _require_embeddings(hub)
        chunks, vecs = _prepare(hub, title, content)
    with tx(conn):
        conn.execute(
            "UPDATE notes SET title=?, content=?, content_hash=?, tags=?, happened_at=?, "
            "updated_at=? WHERE id=?",
            (title, content, hashlib.sha256(content.encode()).hexdigest(), json.dumps(tags),
             happened, _now(), row["id"]),
        )
        if rechunk:
            _write_chunks(hub, conn, row["id"], title, chunks, vecs)
        _audit(conn, actor, "update", uid)
    _mirror(hub, conn, uid)
    return to_dict(conn, _row(conn, uid))


def supersede_note(hub: Hub, conn, uid: str, data: dict, actor: str = "ui") -> dict:
    _require_embeddings(hub)
    old = _row(conn, uid)
    if old["status"] != "current":
        raise ApiError("conflict", "only a current note can be superseded", 409)
    content = _check_content(data.get("content"))
    title = _check_title(data.get("title"), content)
    tags = _clean_tags(data.get("tags")) if "tags" in data else json.loads(old["tags"])
    happened = _clean_date(data["happened_at"]) if "happened_at" in data else old["happened_at"]
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise validation("reason must be a string")
    chunks, vecs = _prepare(hub, title, content)
    source = actor if actor.startswith("agent:") else "ui"
    with tx(conn):
        nid, new_uid = _insert(hub, conn, title=title, content=content, tags=tags,
                               happened_at=happened, source=source, chunks=chunks, vecs=vecs)
        conn.execute(
            "UPDATE notes SET status='superseded', superseded_by=?, status_reason=?, "
            "updated_at=? WHERE id=?",
            (nid, reason, _now(), old["id"]),
        )
        _audit(conn, actor, "supersede", uid, new_uid)
    _mirror(hub, conn, uid)
    _mirror(hub, conn, new_uid)
    return to_dict(conn, _row(conn, new_uid))


def set_status(hub: Hub, conn, uid: str, status: str, reason=None, actor: str = "ui") -> dict:
    row = _row(conn, uid)
    if status == "archived" and row["status"] == "archived":
        raise ApiError("conflict", "already archived", 409)
    if status == "current" and row["status"] != "archived":
        raise ApiError("conflict", "only archived notes can be restored", 409)
    with tx(conn):
        conn.execute(
            "UPDATE notes SET status=?, status_reason=?, updated_at=? WHERE id=?",
            (status, reason, _now(), row["id"]),
        )
        _audit(conn, actor, "archive" if status == "archived" else "restore", uid)
    _mirror(hub, conn, uid)
    return to_dict(conn, _row(conn, uid))


def forget_note(hub: Hub, conn, uid: str, actor: str = "ui", reason: str | None = None) -> None:
    row = _row(conn, uid)
    with tx(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM chunks WHERE owner_kind='note' AND owner_id=?", (row["id"],))]
        hub.store.delete(conn, ids)
        conn.execute("DELETE FROM chunks WHERE owner_kind='note' AND owner_id=?", (row["id"],))
        conn.execute("DELETE FROM notes WHERE id=?", (row["id"],))
        _audit(conn, actor, "forget", uid, reason)
    mirror.remove(hub.settings.data_dir, uid)
