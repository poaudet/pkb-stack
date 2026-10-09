from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import entities as svc
from .. import entity_types as type_svc
from ..errors import validation
from .notes import _actor, _body, _page

bp = Blueprint("entities", __name__)


def _run(fn):
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return fn(hub, conn)
    finally:
        conn.close()


@bp.get("/api/entity-types")
def types_list():
    return _run(lambda h, c: jsonify({"types": svc.list_types(c)}))


@bp.post("/api/entity-types")
def types_create():
    data = _body()
    return _run(lambda h, c: (jsonify({"type": svc.create_type(c, data, _actor())}), 201))


@bp.get("/api/entity-types/<name>")
def types_get(name: str):
    return _run(lambda h, c: jsonify({"type": svc.get_type(c, name)}))


@bp.post("/api/entity-types/<name>/extend")
def types_extend(name: str):
    data = _body()
    if set(data) != {"add_properties", "confirm"} or data.get("confirm") is not True:
        raise validation("add_properties and confirm=true are required")
    def extend_type(hub, conn):
        result = type_svc.extend(
            conn, name, data["add_properties"], actor=_actor(), max_new=20
        )
        return jsonify({key: result[key] for key in ("type", "version", "added")})

    return _run(extend_type)


@bp.get("/api/entity-types/<name>/history")
def types_history(name: str):
    return _run(lambda h, c: jsonify(type_svc.history(c, name)))


@bp.patch("/api/entity-types/<name>")
def types_patch(name: str):
    data = _body()
    force = request.args.get("force") == "true"
    return _run(lambda h, c: jsonify({"type": svc.patch_type(c, name, data, force, _actor())}))


@bp.delete("/api/entity-types/<name>")
def types_delete(name: str):
    return _run(lambda h, c: (svc.delete_type(c, name, _actor()), jsonify({"ok": True}))[1])


@bp.post("/api/entities")
def create():
    data = _body()
    t = data.get("type")
    if not isinstance(t, str):
        raise validation("type is required")
    return _run(lambda h, c: (
        jsonify({"entity": svc.create_entity(h, c, t, data.get("data"), _actor())}), 201))


@bp.get("/api/entities")
def list_():
    limit, offset = _page()
    return _run(lambda h, c: jsonify(svc.list_entities(
        c, request.args.get("type"), request.args.get("status"), limit, offset)))


@bp.post("/api/entities/query")
def query():
    data = _body()
    return _run(lambda h, c: jsonify(svc.query(c, data)))


@bp.get("/api/entities/<uid>")
def get(uid: str):
    return _run(lambda h, c: jsonify({"entity": svc.get_entity(c, uid)}))


@bp.patch("/api/entities/<uid>")
def patch(uid: str):
    data = _body()
    force = request.args.get("force") == "true"
    return _run(lambda h, c: jsonify(
        {"entity": svc.patch_entity(h, c, uid, data, _actor(), force=force)}))


@bp.post("/api/entities/<uid>/supersede")
def supersede(uid: str):
    data = _body()
    force = request.args.get("force") == "true"
    return _run(lambda h, c: (jsonify({"entity": svc.supersede_entity(
        h, c, uid, data.get("data"), data.get("reason"), _actor(), force=force)}), 201))


@bp.post("/api/entities/<uid>/archive")
def archive(uid: str):
    reason = (request.get_json(silent=True) or {}).get("reason")
    force = request.args.get("force") == "true"
    return _run(lambda h, c: jsonify({"entity": svc.set_status(c, uid, "archived", reason,
                                                               _actor(), force=force)}))


@bp.post("/api/entities/<uid>/restore")
def restore(uid: str):
    return _run(lambda h, c: jsonify({"entity": svc.set_status(c, uid, "current", None,
                                                               _actor())}))


@bp.delete("/api/entities/<uid>")
def forget(uid: str):
    if request.args.get("confirm") != "true":
        raise validation("confirm=true is required to forget an entity")
    force = request.args.get("force") == "true"
    return _run(lambda h, c: (
        svc.forget_entity(h, c, uid, _actor(), request.args.get("reason"), force=force),
        jsonify({"ok": True}),
    )[1])
