from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import notes as svc
from .. import pending
from ..errors import validation

bp = Blueprint("notes", __name__)


def _hub():
    return current_app.extensions["hub"]


def _actor() -> str:
    return (request.headers.get("X-Actor") or "ui")[:80]


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise validation("JSON object body required")
    return data


def _page() -> tuple[int, int]:
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
    except ValueError as e:
        raise validation("limit/offset must be integers") from e
    return max(1, min(limit, 200)), max(0, offset)


@bp.post("/api/notes")
def create():
    data = _body()
    hub = _hub()
    conn = hub.conn()
    try:
        agent = _actor().startswith("agent:")
        if data.get("preprocess"):
            pending.require_llm(hub)
            return jsonify({"pending": pending.create(hub, conn, data, _actor(), queue=True)}), 202
        if agent and hub.settings.agent_notes_require_review:
            return jsonify({"pending": pending.create(hub, conn, data, _actor(), queue=False)}), 202
        return jsonify({"note": svc.create_note(hub, conn, data, _actor())}), 201
    finally:
        conn.close()


@bp.get("/api/notes")
def list_():
    limit, offset = _page()
    conn = _hub().conn()
    try:
        return jsonify(svc.list_notes(conn, request.args.get("status"), request.args.get("tag"),
                                      request.args.get("q"), limit, offset))
    finally:
        conn.close()


@bp.get("/api/notes/<uid>")
def get(uid: str):
    conn = _hub().conn()
    try:
        return jsonify({"note": svc.get_note(conn, uid)})
    finally:
        conn.close()


@bp.patch("/api/notes/<uid>")
def patch(uid: str):
    hub = _hub()
    conn = hub.conn()
    try:
        return jsonify({"note": svc.patch_note(hub, conn, uid, _body(), _actor())})
    finally:
        conn.close()


@bp.post("/api/notes/<uid>/supersede")
def supersede(uid: str):
    hub = _hub()
    conn = hub.conn()
    try:
        return jsonify({"note": svc.supersede_note(hub, conn, uid, _body(), _actor())}), 201
    finally:
        conn.close()


@bp.post("/api/notes/<uid>/archive")
def archive(uid: str):
    hub = _hub()
    conn = hub.conn()
    try:
        reason = (request.get_json(silent=True) or {}).get("reason")
        return jsonify({"note": svc.set_status(hub, conn, uid, "archived", reason, _actor())})
    finally:
        conn.close()


@bp.post("/api/notes/<uid>/restore")
def restore(uid: str):
    hub = _hub()
    conn = hub.conn()
    try:
        return jsonify({"note": svc.set_status(hub, conn, uid, "current", None, _actor())})
    finally:
        conn.close()


@bp.delete("/api/notes/<uid>")
def forget(uid: str):
    if request.args.get("confirm") != "true":
        raise validation("confirm=true is required to forget a note")
    hub = _hub()
    conn = hub.conn()
    try:
        svc.forget_note(hub, conn, uid, _actor(), request.args.get("reason"))
        return jsonify({"ok": True})
    finally:
        conn.close()
