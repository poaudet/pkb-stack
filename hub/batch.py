"""Batch writes: many create/update/supersede/archive operations in one request.

All operations run in one transaction (atomic) or one savepoint each (partial). Embeddings
are computed after the commit in a single batch; a failure there never undoes saved data
(a reindex fills in missing vectors).
"""
from __future__ import annotations

import logging

from . import entities, guard, notes, policy
from .db import tx
from .errors import ApiError, validation

log = logging.getLogger(__name__)

MODES = ("atomic", "partial")
OPS = ("create_entity", "update_entity", "supersede_entity", "archive_entity", "create_note")


class _DeferredEmbedder:
    def __init__(self, real) -> None:
        self._real = real

    def embed_documents(self, texts: list[str]) -> list:
        return [None] * len(texts)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class _DeferredStore:
    def __init__(self, real) -> None:
        self._real = real
        self.pending: list[int] = []

    def upsert(self, conn, chunk_id: int, vec) -> None:
        self.pending.append(chunk_id)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class _BatchHub:
    """Hub view whose embeddings are deferred until after the batch commits."""

    def __init__(self, hub) -> None:
        self._hub = hub
        self.embedder = _DeferredEmbedder(hub.embedder)
        self.store = _DeferredStore(hub.store)

    def __getattr__(self, name: str):
        return getattr(self._hub, name)


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _guard(value: object) -> None:
    for text in _strings(value):
        finding = guard.find_block(text)
        if finding:
            raise guard.blocked_error(finding)


def _need(op: dict, key: str, kind: type) -> object:
    value = op.get(key)
    if not isinstance(value, kind):
        raise validation(f"{key} must be a {kind.__name__}")
    return value


def _agent_check(conn, op: dict, actor: str, *, uid_key: str | None, type_key: str | None) -> None:
    if not actor.startswith("agent:"):
        return
    if uid_key:
        policy.require_entity_agent_visible(conn, _need(op, uid_key, str))
    if type_key:
        policy.require_agent_visible(conn, _need(op, type_key, str))


def _run_op(hub, conn, op: dict, actor: str, queue: list[str]) -> object:
    name = op.get("op")
    if name == "create_entity":
        _agent_check(conn, op, actor, uid_key=None, type_key="type")
        _guard(op.get("data"))
        return {"entity": entities.create_entity(
            hub, conn, _need(op, "type", str), op.get("data"), actor, in_tx=True)}
    if name == "update_entity":
        _agent_check(conn, op, actor, uid_key="id", type_key=None)
        _guard(op.get("patch"))
        return {"entity": entities.patch_entity(
            hub, conn, _need(op, "id", str), op.get("patch"), actor, in_tx=True)}
    if name == "supersede_entity":
        _agent_check(conn, op, actor, uid_key="id", type_key=None)
        _guard(op.get("data"))
        return {"entity": entities.supersede_entity(
            hub, conn, _need(op, "id", str), op.get("data"), op.get("reason"), actor, in_tx=True)}
    if name == "archive_entity":
        _agent_check(conn, op, actor, uid_key="id", type_key=None)
        return {"entity": entities.set_status(
            conn, _need(op, "id", str), "archived", op.get("reason"), actor, in_tx=True)}
    if name == "create_note":
        if actor.startswith("agent:") and hub.settings.agent_notes_require_review:
            raise ApiError("review_required",
                           "agent notes need review; use POST /api/notes for those", 409)
        data = {key: op[key] for key in ("content", "title", "tags", "happened_at",
                                         "allow_duplicate") if key in op}
        _guard([data.get("content"), data.get("title")])
        return {"note": notes.create_note(hub, conn, data, actor, outer_tx=True,
                                          mirror_queue=queue)}
    raise validation(f"unknown op {name!r}; expected one of {', '.join(OPS)}")


def _finish(entry: dict, op: dict) -> dict:
    """Add the caller's ref, a status word and the affected record id to a result."""
    entry["status"] = ("skipped" if entry.get("skipped") else "ok" if entry["ok"] else "error")
    if isinstance(op.get("ref"), str):
        entry["ref"] = op["ref"]
    result = entry.get("result")
    record = result.get("entity") or result.get("note") if isinstance(result, dict) else None
    if isinstance(record, dict) and "id" in record:
        entry["id"] = record["id"]
    return entry


def _error(exc: ApiError) -> dict:
    return {"code": exc.code, "message": exc.message, **exc.extra}


def validate_envelope(body: object, max_ops: int) -> tuple[list[dict], str, bool]:
    if not isinstance(body, dict):
        raise validation("JSON object body required")
    if set(body) - {"ops", "mode", "dry_run"}:
        raise validation("only ops, mode and dry_run are accepted")
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        raise validation("ops must be a non-empty array")
    mode = body.get("mode", "atomic")
    if mode not in MODES:
        raise validation(f"mode must be one of {', '.join(MODES)}")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise validation("dry_run must be a boolean")
    if len(ops) > max_ops:
        raise ApiError("too_large", f"at most {max_ops} operations per batch", 413,
                       max_ops=max_ops)
    if not all(isinstance(op, dict) for op in ops):
        raise validation("every op must be an object")
    return ops, mode, dry_run


def run(hub, conn, body: object, actor: str) -> dict:
    ops, mode, dry_run = validate_envelope(body, hub.settings.batch_max_ops)
    if hub.needs_reindex:
        raise ApiError("needs_reindex", "embedding model changed; reindex required", 409)
    bhub = _BatchHub(hub)
    queue: list[str] = []
    results: list[dict] = []
    failed = False
    conn.execute("BEGIN IMMEDIATE")
    try:
        for index, op in enumerate(ops):
            entry = {"index": index, "op": op.get("op")}
            if "id" in op and not isinstance(op["id"], str):
                entry.update(ok=False, error=_error(validation("id must be a string")))
                failed = True
            elif failed and mode == "atomic":
                entry.update(ok=False, skipped=True)
            else:
                conn.execute("SAVEPOINT batch_op")
                try:
                    entry.update(ok=True, result=_run_op(bhub, conn, op, actor, queue))
                    conn.execute("RELEASE batch_op")
                except ApiError as exc:
                    conn.execute("ROLLBACK TO batch_op")
                    conn.execute("RELEASE batch_op")
                    entry.update(ok=False, error=_error(exc))
                    failed = True
            results.append(_finish(entry, op))
        commit = not dry_run and not (failed and mode == "atomic")
        conn.execute("COMMIT" if commit else "ROLLBACK")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    embeddings = {"pending": 0, "failed": 0}
    if commit:
        for uid in dict.fromkeys(queue):
            try:
                notes._mirror(hub, conn, uid)
            except Exception:  # noqa: BLE001 - mirror is best effort after commit
                log.warning("markdown mirror failed for a note")
        embeddings = _embed(hub, conn, bhub.store.pending)
    return {"ok": not failed, "mode": mode, "dry_run": dry_run, "committed": commit,
            "results": results, "embeddings": embeddings}


def _embed(hub, conn, chunk_ids: list[int]) -> dict:
    """Embed new chunks in one call; never raises, never rolls back saved data."""
    unique = list(dict.fromkeys(chunk_ids))
    if not unique:
        return {"pending": 0, "failed": 0}
    try:
        rows = []
        for cid in unique:
            row = conn.execute("SELECT id, title, text FROM chunks WHERE id=?", (cid,)).fetchone()
            if row:
                rows.append(row)
        vecs = hub.embedder.embed_documents([f"{row['title']}\n{row['text']}" for row in rows])
        with tx(conn):
            for row, vec in zip(rows, vecs, strict=True):
                hub.store.upsert(conn, row["id"], vec)
        return {"pending": 0, "failed": 0}
    except Exception:  # noqa: BLE001 - data is committed; a reindex repairs vectors
        log.warning("batch embedding failed for %d chunks; run a reindex", len(unique))
        return {"pending": len(unique), "failed": len(unique)}
