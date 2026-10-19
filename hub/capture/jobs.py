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
from ..config import Settings
from ..core import Hub
from ..db import tx
from ..errors import ApiError, not_found, validation
from . import context, gates, llm, plan, redact
from .prompt import messages as build_messages

log = logging.getLogger(__name__)
EDITABLE = {"ready", "failed"}
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
    digest = gates.gate(
        conn, hub.settings, text,
        allow_secrets=data.get("allow_secrets") is True,
        allow_duplicate=data.get("allow_duplicate") is True,
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
            "INSERT INTO captures(uid,raw_text,text_hash,hint,as_of,source,state,redacted_kinds) "
            "VALUES(?,?,?,?,?,?,'queued',?)",
            (uid, text, digest, hint, as_of, source, json.dumps(redacted_kinds)),
        )
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid) VALUES(?,?,?,?)",
            (actor, "capture_create", "capture", uid),
        )
    hub.capture_queue.submit(uid)
    return {"capture": to_dict(_row(conn, uid))}, 202


def detail(conn: sqlite3.Connection, uid: str) -> dict:
    return to_dict(_row(conn, uid))


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
    hub.capture_queue.submit(uid)
    return to_dict(_row(conn, uid))


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
                try:
                    ZoneInfo(self.hub.settings.hub_timezone)
                except ZoneInfoNotFoundError as exc:
                    raise ValueError("HUB_TIMEZONE must be a valid timezone") from exc
                capture_settings = self.hub.llm_settings("capture")
                catalog = context.build_catalog(
                    conn, self.hub.settings.capture_exclude_types,
                    self.hub.settings.capture_catalog_max_chars,
                )
                candidates = context.build_candidates(
                    self.hub, conn, raw_text, self.hub.settings.capture_candidates,
                    self.hub.settings.capture_exclude_types,
                )
                max_ops = max(1, min(20, self.hub.settings.capture_max_ops))
                msgs = build_messages(
                    text=raw_text, hint=row["hint"], answers=json.loads(row["answers"]),
                    today=row["as_of"], catalog=catalog,
                    candidates=context.candidate_lines(
                        candidates,
                        context.self_line(conn, self.hub.settings.capture_exclude_types),
                    ),
                    self_line=context.self_line(
                        conn, self.hub.settings.capture_exclude_types,
                    ), max_ops=max_ops,
                    tz=self.hub.settings.hub_timezone,
                )
                if sum(len(message["content"]) for message in msgs) > 24_000:
                    raise ValueError("capture prompt exceeds 24000 characters")

                last_result = None

                def request_plan(messages):
                    nonlocal last_result
                    result = llm.call(
                        capture_settings, messages,
                        lambda: gates.consume_call(conn, self.hub.settings),
                    )
                    last_result = result
                    parsed = _parse_reply(result.text)
                    plan.validate(parsed)
                    normalized = plan.normalize(
                        conn, parsed, raw_text, candidates, max_ops=max_ops,
                        settings=self.hub.settings,
                    )
                    return result, normalized

                total_prompt = total_completion = None
                try:
                    result, normalized = request_plan(msgs)
                    total_prompt, total_completion = result.prompt_tokens, result.completion_tokens
                except ValueError as exc:
                    errors = str(exc)[:1000]
                    assistant = last_result.text if last_result else ""
                    total_prompt = last_result.prompt_tokens if last_result else None
                    total_completion = last_result.completion_tokens if last_result else None
                    repair = [
                        *msgs,
                        {"role": "assistant", "content": assistant},
                        {"role": "user", "content":
                         f"Return the corrected JSON object only. Validation errors: {errors}"},
                    ]
                    result, normalized = request_plan(repair)
                    total_prompt = _sum_tokens(total_prompt, result.prompt_tokens)
                    total_completion = _sum_tokens(total_completion, result.completion_tokens)
                with tx(conn):
                    conn.execute(
                        "UPDATE captures SET state='ready',plan=?,questions=?,warnings=?,error=NULL,"
                        "llm_model=?,prompt_tokens=?,completion_tokens=? "
                        "WHERE uid=? AND state='processing'",
                        (json.dumps(normalized, ensure_ascii=False),
                         json.dumps(normalized["questions"], ensure_ascii=False),
                         json.dumps(normalized["warnings"], ensure_ascii=False),
                         result.model, total_prompt, total_completion, uid),
                    )
            except Exception as exc:  # noqa: BLE001 - persist all job failures without leaking data
                self._fail(conn, uid, _safe_error(exc))
        finally:
            conn.close()

    @staticmethod
    def _fail(conn, uid: str, error: str) -> None:
        with tx(conn):
            conn.execute(
                "UPDATE captures SET state='failed',error=? WHERE uid=? AND state='processing'",
                (error[:300], uid),
            )


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
    if isinstance(exc, ValueError):
        return f"invalid plan: {type(exc).__name__}"
    if isinstance(exc, llm.CaptureLLMError):
        return str(exc)[:300]
    return f"capture failed: {type(exc).__name__}"


def _sum_tokens(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    return left + right if right is not None else left
