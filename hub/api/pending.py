from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import pending as svc
from ..errors import validation
from .notes import _actor, _body, _page

bp = Blueprint("pending", __name__)


def _with_conn(fn):
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return fn(hub, conn)
    finally:
        conn.close()


@bp.get("/api/pending")
def list_():
    limit, offset = _page()
    return _with_conn(lambda h, c: jsonify(
        svc.list_pending(c, request.args.get("state"), limit, offset)))


@bp.get("/api/pending/<uid>")
def get(uid: str):
    return _with_conn(lambda h, c: jsonify({"pending": svc.detail(h, c, uid)}))


@bp.patch("/api/pending/<uid>")
def patch(uid: str):
    data = _body()
    return _with_conn(lambda h, c: jsonify({"pending": svc.patch(c, uid, data)}))


@bp.post("/api/pending/<uid>/approve")
def approve(uid: str):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise validation("JSON object body required")
    return _with_conn(lambda h, c: (jsonify(svc.approve(h, c, uid, data, _actor())), 201))


@bp.post("/api/pending/<uid>/reject")
def reject(uid: str):
    return _with_conn(lambda h, c: jsonify({"pending": svc.reject(c, uid, _actor())}))


@bp.post("/api/pending/<uid>/regenerate")
def regenerate(uid: str):
    return _with_conn(lambda h, c: (jsonify({"pending": svc.regenerate(h, c, uid)}), 202))
