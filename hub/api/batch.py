from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import batch

bp = Blueprint("batch", __name__)


@bp.post("/api/batch")
def run():
    hub = current_app.extensions["hub"]
    actor = (request.headers.get("X-Actor") or "api")[:80]
    conn = hub.conn()
    try:
        return jsonify(batch.run(hub, conn, request.get_json(silent=True), actor))
    finally:
        conn.close()
