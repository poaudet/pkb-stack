from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from .. import self as self_svc
from ..errors import validation
from .notes import _actor, _body

bp = Blueprint("self", __name__)


@bp.get("/api/self")
def get_self():
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return jsonify(self_svc.get_self(conn))
    finally:
        conn.close()


@bp.put("/api/self")
def put_self():
    data = _body()
    if set(data) - {"name", "data"}:
        raise validation("unsupported owner field")
    if "name" not in data:
        raise validation("name is required")
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return jsonify(self_svc.set_self(
            hub, conn, data["name"], data.get("data"), _actor()
        ))
    finally:
        conn.close()
