from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import tools
from .notes import _page

bp = Blueprint("tools", __name__)


def _hub():
    return current_app.extensions["hub"]


@bp.get("/api/tools")
def manifest():
    return jsonify(tools.manifest(_hub()))


@bp.post("/api/tools/<name>")
def call(name: str):
    args = request.get_json(silent=True)
    if args is None and not request.get_data():
        args = {}
    # tool callers are agents; an unnamed caller is still tagged as one
    actor = (request.headers.get("X-Actor") or "agent:api")[:80]
    hub = _hub()
    conn = hub.conn()
    try:
        return jsonify(tools.call(hub, conn, name, args, actor))
    finally:
        conn.close()


@bp.get("/api/activity")
def activity():
    limit, offset = _page()
    conn = _hub().conn()
    try:
        rows = conn.execute(
            "SELECT id, at, actor, action, target_kind, target_uid, detail FROM audit_log "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    finally:
        conn.close()
