"""Capture request lifecycle and background processing queue."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Timer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import notes
from .. import self as self_svc
from ..config import Settings
from ..core import Hub
from ..db import tx
from ..errors import ApiError, not_found, validation
from . import context, dates, gates, llm, merge, plan, redact, split
from .prompt import messages as build_messages

log = logging.getLogger(__name__)
EDITABLE = {"ready", "failed"}
LONG_ACTIVE = ("queued", "processing")
STATES = {"queued", "processing", "ready", "failed", "applied", "rejected", "undone"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def configured(settings: Settings) -> bool:
    return settings.capture_enabled and bool(settings.capture_llm_model or settings.llm_model)


def _row(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM captures WHERE uid=?", (uid,)).fetchone()
    if row is None:
        raise not_found("capture not found")
    return row


def to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["uid"],
        "text": row["raw_text"],
        "text_hash": row["text_hash"],
        "hint": row["hint"],
        "as_of": row["as_of"],
        "source": row["source"],
        "state": row["state"],
        "plan": json.loads(row["plan"]),
        "questions": json.loads(row["questions"]),
        "answers": json.loads(row["answers"]),
        "warnings": json.loads(row["warnings"]),
        "redacted_kinds": json.loads(row["redacted_kinds"]),
        "mode": row["mode"],
        "chunks_total": row["chunks_total"],
        "chunks_done": row["chunks_done"],
        "provenance": json.loads(row["op_meta"]).get("provenance", {}),
        "partial": False,
        "chunks": [],
        "error": row["error"],
        "llm_model": row["llm_model"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def create(hub: Hub, conn: sqlite3.Connection, data: dict, actor: str) -> tuple[dict, int]:
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise validation("text is required")
    text, redacted_kinds = redact.redact(text)
    skip_ai = data.get("skip_ai") is True
    if skip_ai:
        if len(text) > hub.settings.capture_max_chars:
            raise ApiError("too_long", "text exceeds CAPTURE_MAX_CHARS; save it as a note", 400)
        note = notes.create_note(
            hub, conn, {"content": text, "source": "capture"},
            actor, source_override="capture",
        )
        return {"note": note}, 201
    if not hub.settings.capture_enabled:
        raise ApiError("capture_disabled", "Smart Capture is disabled", 409)
    if not configured(hub.llm_settings("capture")):
        raise ApiError("llm_not_configured", "Capture LLM is not configured", 409)
    mode = data.get("mode", "auto")
    if mode not in ("auto", "short", "long"):
        raise validation("mode must be auto, short or long")
    settings = hub.settings
    long_enabled = settings.capture_long_mode != "off"
    is_long = long_enabled and (
        mode == "long" or (mode == "auto" and len(text) > settings.capture_long_threshold_chars)
    )
    chunks: list[dict] = []
    if is_long:
        if len(text) > settings.capture_long_max_chars:
            raise ApiError("too_large", "text exceeds CAPTURE_LONG_MAX_CHARS", 413,
                           limit=settings.capture_long_max_chars)
        chunks = split.split_text(text, settings.capture_chunk_target_chars)
        if len(chunks) > settings.capture_long_max_chunks:
            raise ApiError("too_large", "text needs more than CAPTURE_LONG_MAX_CHUNKS chunks",
                           413, limit=settings.capture_long_max_chunks, chunks=len(chunks))
    digest = gates.gate(
        conn, hub.settings, text,
        allow_secrets=data.get("allow_secrets") is True,
        allow_duplicate=data.get("allow_duplicate") is True,
        max_chars=settings.capture_long_max_chars if is_long else None,
    )
    hint = data.get("hint")
    if hint is not None and not isinstance(hint, str):
        raise validation("hint must be a string")
    if hint and len(hint) > 1000:
        raise validation("hint exceeds 1000 characters")
    if hint:
        hint, _ = redact.redact(hint)
    as_of = data.get("as_of")
    if as_of is not None:
        if not isinstance(as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
            raise validation("as_of must be an ISO date")
        try:
            as_of = date.fromisoformat(as_of).isoformat()
        except (ValueError, TypeError) as exc:
            raise validation("as_of must be an ISO date") from exc
    else:
        try:
            as_of = datetime.now(ZoneInfo(hub.settings.hub_timezone)).date().isoformat()
        except ZoneInfoNotFoundError as exc:
            raise validation("HUB_TIMEZONE must be a valid timezone") from exc
    source = actor if actor.startswith("agent:") else ("api" if actor == "api" else "ui")
    uid = uuid.uuid4().hex
    with tx(conn):
        conn.execute(
            "INSERT INTO captures(uid,raw_text,text_hash,hint,as_of,source,state,redacted_kinds,"
            "mode,chunks_total) VALUES(?,?,?,?,?,?,'queued',?,?,?)",
            (uid, text, digest, hint, as_of, source, json.dumps(redacted_kinds),
             "long" if is_long else "short", len(chunks)),
        )
        job_id = conn.execute("SELECT id FROM captures WHERE uid=?", (uid,)).fetchone()["id"]
        conn.executemany(
            "INSERT INTO capture_chunks(job_id,idx,start_char,end_char) VALUES(?,?,?,?)",
            [(job_id, c["idx"], c["start_char"], c["end_char"]) for c in chunks],
        )
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid) VALUES(?,?,?,?)",
            (actor, "capture_create", "capture", uid),
        )
    log.info("capture created: mode=%s chunks=%d redacted=%s", "long" if is_long else "short",
             len(chunks), sum(redacted_kinds.values()))
    hub.capture_queue.submit(uid)
    return {"capture": detail(conn, uid)}, 202


def _chunk_rows(conn: sqlite3.Connection, row: sqlite3.Row) -> list[dict]:
    return [
        {"idx": c["idx"], "start_char": c["start_char"], "end_char": c["end_char"],
         "status": c["status"], "error_code": c["error_code"]}
        for c in conn.execute(
            "SELECT idx,start_char,end_char,status,error_code FROM capture_chunks "
            "WHERE job_id=? ORDER BY idx", (row["id"],))
    ]


def detail(conn: sqlite3.Connection, uid: str) -> dict:
    row = _row(conn, uid)
    out = to_dict(row)
    if row["mode"] == "long":
        out["chunks"] = _chunk_rows(conn, row)
        out["partial"] = any(c["status"] == "failed" for c in out["chunks"])
    return out


def list_captures(conn: sqlite3.Connection, state: str | None, source: str | None, limit: int,
                  offset: int) -> dict:
    conditions = []
    params: list[object] = []
    if state:
        if state not in STATES:
            raise validation("invalid state")
        conditions.append("state=?")
        params.append(state)
    if source:
        conditions.append("source=?")
        params.append(source)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = conn.execute(f"SELECT count(*) FROM captures{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM captures{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    items = [to_dict(row) for row in rows]
    captures = []
    for row, item in zip(rows, items, strict=True):
        plan_data = item["plan"]
        entry = {
            "id": item["id"],
            "state": item["state"],
            "source": item["source"],
            "created_at": item["created_at"],
            "summary": plan_data.get("summary") or (item["text"] or "")[:80],
            "model": item["llm_model"],
            "op_count": len(plan_data.get("ops", [])),
        }
        if item["error"]:
            entry["error"] = item["error"]
        captures.append(entry)
    return {"captures": captures, "items": items, "total": total}


def reject(conn: sqlite3.Connection, uid: str, actor: str) -> dict:
    row = _row(conn, uid)
    if row["state"] not in EDITABLE:
        raise ApiError("capture_state", f"cannot reject a {row['state']} capture", 409)
    with tx(conn):
        conn.execute(
            "UPDATE captures SET state='rejected',decided_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid) VALUES(?,?,?,?)",
            (actor, "capture_reject", "capture", uid),
        )
    return to_dict(_row(conn, uid))


def regenerate(hub: Hub, conn: sqlite3.Connection, uid: str, data: dict) -> dict:
    if not configured(hub.llm_settings("capture")):
        raise ApiError("llm_not_configured", "Capture LLM is not configured", 409)
    row = _row(conn, uid)
    if row["state"] not in EDITABLE:
        raise ApiError("capture_state", f"cannot regenerate a {row['state']} capture", 409)
    if not row["raw_text"]:
        raise ApiError("capture_state", "capture text retention has expired", 409)
    gates.gate(conn, hub.settings, row["raw_text"], allow_duplicate=True)
    answers = data.get("answers", json.loads(row["answers"]))
    hint = data.get("hint", row["hint"])
    if not isinstance(answers, dict) or not isinstance(hint, (str, type(None))):
        raise validation("answers must be an object and hint a string")
    answers = json.loads(redact.redact(json.dumps(answers, ensure_ascii=False))[0])
    if hint:
        hint, _ = redact.redact(hint)
    with tx(conn):
        conn.execute(
            "UPDATE captures SET state='queued',error=NULL,answers=?,hint=? WHERE id=?",
            (json.dumps(answers), hint, row["id"]),
        )
        if row["mode"] == "long":
            conn.execute(
                "UPDATE capture_chunks SET status='pending',plan_json=NULL,error_code=NULL "
                "WHERE job_id=?", (row["id"],))
            conn.execute("UPDATE captures SET chunks_done=0 WHERE id=?", (row["id"],))
    hub.capture_queue.submit(uid)
    return detail(conn, uid)


def _long_row(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    row = _row(conn, uid)
    if row["mode"] != "long":
        raise ApiError("capture_state", "capture is not a long capture", 409)
    return row


def _audit(conn: sqlite3.Connection, actor: str, action: str, uid: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor,action,target_kind,target_uid) VALUES(?,?,?,?)",
        (actor, action, "capture", uid),
    )


def cancel(conn: sqlite3.Connection, uid: str, actor: str) -> dict:
    """Stop after the current chunk; finished chunks are kept."""
    row = _long_row(conn, uid)
    if row["state"] not in LONG_ACTIVE:
        raise ApiError("capture_state", f"cannot cancel a {row['state']} capture", 409)
    with tx(conn):
        conn.execute(
            "UPDATE captures SET state='failed',error='cancelled' WHERE id=?", (row["id"],))
        _audit(conn, actor, "capture_cancel", uid)
    return detail(conn, uid)


def continue_partial(hub: Hub, conn: sqlite3.Connection, uid: str, actor: str) -> dict:
    """Review what the finished chunks produced, leaving failed chunks out."""
    row = _long_row(conn, uid)
    chunks = _chunk_rows(conn, row)
    if row["state"] != "failed" or any(c["status"] == "running" for c in chunks):
        raise ApiError("capture_state", "capture is not waiting on failed chunks", 409)
    if not any(c["status"] == "done" for c in chunks):
        raise ApiError("capture_state", "no chunk has finished yet", 409)
    store_merged(hub, conn, uid, from_states=("failed",))
    with tx(conn):
        _audit(conn, actor, "capture_continue", uid)
    return detail(conn, uid)


def retry_chunk(hub: Hub, conn: sqlite3.Connection, uid: str, idx: int, actor: str) -> dict:
    """Redo one failed chunk from scratch; its new output is normalized and re-merged."""
    row = _long_row(conn, uid)
    if row["state"] not in ("failed", "ready"):
        raise ApiError("capture_state", f"cannot retry a chunk of a {row['state']} capture", 409)
    chunk = conn.execute(
        "SELECT status FROM capture_chunks WHERE job_id=? AND idx=?", (row["id"], idx)
    ).fetchone()
    if chunk is None:
        raise not_found("chunk not found")
    if chunk["status"] != "failed":
        raise ApiError("capture_state", "only a failed chunk can be retried", 409)
    with tx(conn):
        conn.execute(
            "UPDATE capture_chunks SET status='pending',plan_json=NULL,error_code=NULL "
            "WHERE job_id=? AND idx=?", (row["id"], idx))
        conn.execute("UPDATE captures SET state='queued',error=NULL WHERE id=?", (row["id"],))
        _audit(conn, actor, "capture_chunk_retry", uid)
    hub.capture_queue.submit_retry(uid)
    return detail(conn, uid)


def save_note(hub: Hub, conn: sqlite3.Connection, uid: str, actor: str) -> dict:
    row = _row(conn, uid)
    if row["state"] not in EDITABLE or not row["raw_text"]:
        raise ApiError("capture_state", "capture text is not available to save", 409)
    note = notes.create_note(
        hub, conn, {"content": row["raw_text"], "source": "capture"},
        actor, source_override="capture",
    )
    with tx(conn):
        conn.execute(
            "UPDATE captures SET state='rejected',decided_at=? WHERE id=? AND state IN ('ready','failed')",
            (_now(), row["id"]),
        )
    return {"note": note, "capture": to_dict(_row(conn, uid))}


class CaptureQueue:
    """Thread pool for capture plans; LLM calls run outside write transactions."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.pool = ThreadPoolExecutor(
            max_workers=hub.settings.llm_concurrency, thread_name_prefix="capture"
        )
        self._schedule_cleanup()

    def submit(self, uid: str) -> None:
        self.pool.submit(self._run, uid)

    def recover(self) -> None:
        conn = self.hub.conn()
        try:
            retention = max(0, self.hub.settings.capture_keep_raw_days)
            cutoff = (datetime.now(UTC) - timedelta(days=retention)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute("UPDATE captures SET raw_text=NULL WHERE created_at<?", (cutoff,))
            if not configured(self.hub.llm_settings("capture")):
                return
            conn.execute("UPDATE captures SET state='queued' WHERE state='processing'")
            queued = [row["uid"] for row in conn.execute(
                "SELECT uid FROM captures WHERE state='queued' ORDER BY id"
            )]
        finally:
            conn.close()
        for uid in queued:
            self.submit(uid)

    def _schedule_cleanup(self) -> None:
        timer = Timer(24 * 60 * 60, self._cleanup)
        timer.daemon = True
        timer.start()

    def _cleanup(self) -> None:
        conn = self.hub.conn()
        try:
            retention = max(0, self.hub.settings.capture_keep_raw_days)
            cutoff = (datetime.now(UTC) - timedelta(days=retention)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            conn.execute("UPDATE captures SET raw_text=NULL WHERE created_at<?", (cutoff,))
        finally:
            conn.close()
            self._schedule_cleanup()

    def _run(self, uid: str) -> None:
        try:
            self._job(uid)
        except Exception as exc:  # noqa: BLE001 - worker errors do not escape the pool
            log.error("capture job failed: %s", type(exc).__name__)

    def submit_retry(self, uid: str) -> None:
        self.pool.submit(self._run, uid)

    def _job(self, uid: str) -> None:
        conn = self.hub.conn()
        try:
            updated = conn.execute(
                "UPDATE captures SET state='processing' WHERE uid=? AND state='queued'", (uid,)
            ).rowcount
            if updated != 1:
                return
            row = _row(conn, uid)
            raw_text = row["raw_text"]
            if not raw_text:
                self._fail(conn, uid, "capture text retention has expired")
                return
            try:
                if row["mode"] == "long":
                    self._long_job(conn, row)
                    return
                out = extract_plan(
                    self.hub, conn, text=raw_text, hint=row["hint"],
                    answers=json.loads(row["answers"]), today=row["as_of"],
                    max_ops=max(1, min(20, self.hub.settings.capture_max_ops)),
                )
                normalized = out["plan"]
                with tx(conn):
                    conn.execute(
                        "UPDATE captures SET state='ready',plan=?,questions=?,warnings=?,error=NULL,"
                        "llm_model=?,prompt_tokens=?,completion_tokens=? "
                        "WHERE uid=? AND state='processing'",
                        (json.dumps(normalized, ensure_ascii=False),
                         json.dumps(normalized["questions"], ensure_ascii=False),
                         json.dumps(normalized["warnings"], ensure_ascii=False),
                         out["model"], out["prompt_tokens"], out["completion_tokens"], uid),
                    )
            except Exception as exc:  # noqa: BLE001 - persist all job failures without leaking data
                self._fail(conn, uid, _safe_error(exc))
        finally:
            conn.close()

    def _long_job(self, conn, row) -> None:
        job_id, uid = row["id"], row["uid"]
        text = row["raw_text"]
        with tx(conn):
            conn.execute(
                "UPDATE capture_chunks SET status='pending' WHERE job_id=? AND status='running'",
                (job_id,),
            )
        chunks = _chunk_rows(conn, row)
        max_ops = max(1, min(20, self.hub.settings.capture_max_ops))
        for chunk in chunks:
            if chunk["status"] != "pending":
                continue
            state = conn.execute("SELECT state FROM captures WHERE id=?", (job_id,)).fetchone()
            if state is None or state["state"] != "processing":
                log.info("capture long job stopped: chunk=%d", chunk["idx"])
                return
            self._run_chunk(conn, row, chunks, chunk, text, max_ops)
        finalize_long(self.hub, conn, uid)

    def _run_chunk(self, conn, row, chunks, chunk, text, max_ops) -> None:
        job_id, idx = row["id"], chunk["idx"]
        settings = self.hub.settings
        with tx(conn):
            conn.execute(
                "UPDATE capture_chunks SET status='running',error_code=NULL "
                "WHERE job_id=? AND idx=?", (job_id, idx))
        body = text[chunk["start_char"]:chunk["end_char"]]
        info = {
            "index": idx, "total": len(chunks),
            "working_set": _working_set(conn, job_id, idx),
            "context": split.overlap_context(
                text, chunks, idx, settings.capture_chunk_overlap_chars),
        }
        known = {line.split(" | ")[2]: line.split(" | ")[2]
                 for line in info["working_set"].splitlines() if line.count(" | ") == 2}
        try:
            out = extract_plan(
                self.hub, conn, text=body, hint=row["hint"],
                answers=json.loads(row["answers"]), today=row["as_of"], max_ops=max_ops,
                chunk=info, known_refs=known, max_attempts=3,
            )
            normalized = out["plan"]
            normalized["usage"] = {k: out[k] for k in ("model", "prompt_tokens", "completion_tokens")}
            status, code, payload = "done", None, json.dumps(normalized, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - one failing chunk must not stop the job
            status, code, payload = "failed", chunk_error_code(exc), None
            log.warning("capture chunk failed: chunk=%d code=%s", idx, code)
        with tx(conn):
            conn.execute(
                "UPDATE capture_chunks SET status=?,plan_json=?,error_code=? "
                "WHERE job_id=? AND idx=?", (status, payload, code, job_id, idx))
            conn.execute(
                "UPDATE captures SET chunks_done=(SELECT count(*) FROM capture_chunks "
                "WHERE job_id=? AND status='done') WHERE id=?", (job_id, job_id))

    @staticmethod
    def _fail(conn, uid: str, error: str) -> None:
        with tx(conn):
            conn.execute(
                "UPDATE captures SET state='failed',error=? WHERE uid=? AND state='processing'",
                (error[:300], uid),
            )


class SchemaInvalid(ValueError):
    """The model reply failed schema validation twice."""


def chunk_error_code(exc: Exception) -> str:
    if isinstance(exc, SchemaInvalid):
        return "schema_invalid"
    if isinstance(exc, ApiError):
        return exc.code
    if isinstance(exc, llm.CaptureLLMError):
        return f"llm_http_{exc.status_code}" if exc.status_code else "llm_error"
    return "capture_failed"


def _working_set(conn, job_id: int, before_idx: int) -> str:
    lines: list[str] = []
    for item in conn.execute(
        "SELECT plan_json FROM capture_chunks WHERE job_id=? AND idx<? AND status='done' "
        "ORDER BY idx", (job_id, before_idx)
    ):
        for op in json.loads(item["plan_json"]).get("ops", []):
            data = op.get("data") if isinstance(op.get("data"), dict) else {}
            label = next((str(data[k]) for k in ("name", "title") if data.get(k)), "")
            if (op.get("kind") == "create_entity" and op.get("ref") and label
                    and op.get("status") not in ("invalid", "rejected")):
                lines.append(f"{op['type']} | {label[:60]} | {op['ref']}")
    return "\n".join(lines[-60:])


def renormalize(hub, conn, capture_row, raw_plan: dict, *, note_first: bool | None = None) -> dict:
    """Normalize a stored or merged plan with the same rules used for a fresh one."""
    owner = self_svc.get_self(conn)["self"]
    for raw_op in raw_plan["ops"]:
        if owner and raw_op.get("target") == owner["id"]:
            raw_op["target"] = "SELF"
    raw_text = capture_row["raw_text"] or ""
    candidates = context.build_candidates(
        hub, conn, raw_text, hub.settings.capture_candidates,
        hub.settings.capture_exclude_types,
    ) if raw_text else []
    long_mode = capture_row["mode"] == "long"
    base = max(1, min(20, hub.settings.capture_max_ops))
    return plan.normalize(
        conn, raw_plan, raw_text, plan.with_target_candidates(candidates, raw_plan["ops"]),
        max_ops=max(base, len(raw_plan["ops"]) + 1) if long_mode else base,
        settings=hub.settings, note_first=long_mode if note_first is None else note_first,
    )


def finalize_long(hub, conn, uid: str) -> None:
    """After the last chunk: merge when all chunks are done, otherwise report partial."""
    row = _row(conn, uid)
    states = [c["status"] for c in _chunk_rows(conn, row)]
    if any(item in ("pending", "running") for item in states):
        return
    if "failed" in states:
        with tx(conn):
            conn.execute(
                "UPDATE captures SET state='failed',error='partial: some chunks failed' "
                "WHERE uid=? AND state='processing'", (uid,))
        return
    store_merged(hub, conn, uid, from_states=("processing",))


def store_merged(hub, conn, uid: str, from_states: tuple[str, ...]) -> None:
    row = _row(conn, uid)
    chunk_plans = [
        (item["idx"], json.loads(item["plan_json"]))
        for item in conn.execute(
            "SELECT idx,plan_json FROM capture_chunks WHERE job_id=? AND status='done' "
            "ORDER BY idx", (row["id"],))
    ]
    base = max(1, min(20, hub.settings.capture_max_ops))
    ops, provenance, op_warnings, plan_warnings = merge.merge(
        chunk_plans, min(400, base * max(1, row["chunks_total"])))
    questions = list(dict.fromkeys(
        q for _idx, p in chunk_plans for q in p.get("questions", [])))[:5]
    raw_plan = {
        "summary": f"Long capture: {len(ops)} entries from {len(chunk_plans)} chunks",
        "ops": ops, "keep_note": True, "note": None, "questions": questions,
    }
    normalized = renormalize(hub, conn, row, raw_plan, note_first=True)
    for item in normalized["ops"]:
        item["warnings"].extend(op_warnings.get(item["id"], []))
    normalized["warnings"] = [*plan_warnings, *normalized["warnings"]]
    usage = [p.get("usage", {}) for _idx, p in chunk_plans]
    prompt = sum(u.get("prompt_tokens") or 0 for u in usage) or None
    completion = sum(u.get("completion_tokens") or 0 for u in usage) or None
    marks = ",".join("?" for _ in from_states)
    with tx(conn):
        conn.execute(
            "UPDATE captures SET state='ready',plan=?,questions=?,warnings=?,error=NULL,"
            "llm_model=?,prompt_tokens=?,completion_tokens=?,op_meta=? "
            f"WHERE uid=? AND state IN ({marks})",
            (json.dumps(normalized, ensure_ascii=False),
             json.dumps(normalized["questions"], ensure_ascii=False),
             json.dumps(normalized["warnings"], ensure_ascii=False),
             next((u.get("model") for u in usage if u.get("model")), None),
             prompt, completion, json.dumps({"provenance": provenance}), uid, *from_states),
        )
    log.info("capture merged: chunks=%d ops=%d", len(chunk_plans), len(normalized["ops"]))


def extract_plan(hub, conn, *, text: str, hint: str | None, answers: dict, today: str,
                 max_ops: int, chunk: dict | None = None, known_refs: dict | None = None,
                 max_attempts: int = 2) -> dict:
    """Redact, resolve dates and candidates, call the model and normalize one plan."""
    try:
        ZoneInfo(hub.settings.hub_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("HUB_TIMEZONE must be a valid timezone") from exc
    capture_settings = hub.llm_settings("capture")
    text, _ = redact.redact(text)
    day = date.fromisoformat(today)
    resolved_text, _ = dates.resolve(text, day)
    resolved_hint = dates.resolve(redact.redact(hint)[0], day)[0] if hint else hint
    exclude = hub.settings.capture_exclude_types
    catalog = context.build_catalog(conn, exclude, hub.settings.capture_catalog_max_chars)
    candidates = context.build_candidates(
        hub, conn, text, hub.settings.capture_candidates, exclude)
    owner_line = context.self_line(conn, exclude)
    if chunk:
        chunk = {**chunk, "context": dates.resolve(redact.redact(chunk["context"])[0], day)[0]}
    msgs = build_messages(
        text=resolved_text, hint=resolved_hint, answers=answers, today=today,
        catalog=catalog, candidates=context.candidate_lines(candidates, owner_line),
        self_line=owner_line, max_ops=max_ops, tz=hub.settings.hub_timezone, chunk=chunk,
    )
    if sum(len(message["content"]) for message in msgs) > 24_000:
        raise ValueError("capture prompt exceeds 24000 characters")

    last_result = None
    schema = plan.plan_schema()

    def request_plan(messages):
        nonlocal last_result
        result = llm.call(
            capture_settings, messages,
            lambda: gates.consume_call(conn, hub.settings),
            max_attempts=max_attempts, schema=schema,
        )
        last_result = result
        parsed = _parse_reply(result.text)
        plan.validate(parsed)
        if chunk:
            parsed = {**parsed, "keep_note": False, "note": None}
        normalized = plan.normalize(
            conn, parsed, text, candidates, max_ops=max_ops, settings=hub.settings,
            known_refs=known_refs,
        )
        if chunk:
            merge.prefix_plan(normalized, chunk["index"])
        return result, normalized

    try:
        result, normalized = request_plan(msgs)
        prompt_tokens, completion_tokens = result.prompt_tokens, result.completion_tokens
    except ValueError as exc:
        errors = str(exc)[:1000]
        assistant = last_result.text if last_result else ""
        prompt_tokens = last_result.prompt_tokens if last_result else None
        completion_tokens = last_result.completion_tokens if last_result else None
        repair = [
            *msgs,
            {"role": "assistant", "content": assistant},
            {"role": "user", "content":
             f"Return the corrected JSON object only. Validation errors: {errors}"},
        ]
        try:
            result, normalized = request_plan(repair)
        except ValueError as again:
            raise SchemaInvalid(str(again)[:300]) from again
        prompt_tokens = _sum_tokens(prompt_tokens, result.prompt_tokens)
        completion_tokens = _sum_tokens(completion_tokens, result.completion_tokens)
    return {"plan": normalized, "model": result.model, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens}


def _parse_reply(text: str) -> dict:
    import re

    clean = re.sub(r"```(?:json)?", "", text or "")
    start, end = clean.find("{"), clean.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("reply has no JSON object")
    parsed = json.loads(clean[start:end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("reply is not an object")
    return parsed


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return exc.code
    if isinstance(exc, SchemaInvalid):
        return "invalid plan: schema_invalid"
    if isinstance(exc, ValueError):
        return f"invalid plan: {type(exc).__name__}"
    if isinstance(exc, llm.CaptureLLMError):
        return str(exc)[:300]
    return f"capture failed: {type(exc).__name__}"


def _sum_tokens(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    return left + right if right is not None else left
