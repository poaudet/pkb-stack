from __future__ import annotations

from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

from .. import llm_endpoints
from ..errors import validation

bp = Blueprint("llm_settings", __name__)


def _payload(conn, settings) -> dict:
    eps = llm_endpoints.parse(settings.llm_endpoints)
    out = []
    for e in eps:
        host = urlsplit(e.base_url).hostname if e.base_url else None
        out.append({"name": e.name, "model": e.model, "host": host})
    sel = {}
    for purpose in llm_endpoints.PURPOSES:
        ep = llm_endpoints.selected(conn, settings, purpose)
        sel[purpose] = ep.name if ep else None
    return {"endpoints": out, "selected": sel}


@bp.get("/api/settings/llm")
def get_llm_settings():
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return jsonify(_payload(conn, hub.settings))
    finally:
        conn.close()


@bp.put("/api/settings/llm")
def put_llm_settings():
    hub = current_app.extensions["hub"]
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise validation("body must be a JSON object")
    conn = hub.conn()
    try:
        for purpose in llm_endpoints.PURPOSES:
            if purpose not in data:
                continue
            name = data[purpose]
            if not isinstance(name, str):
                raise validation(f"{purpose} must be an endpoint name")
            try:
                llm_endpoints.select(conn, hub.settings, purpose, name)
            except KeyError:
                raise validation(f"unknown endpoint for {purpose}") from None
        return jsonify(_payload(conn, hub.settings))
    finally:
        conn.close()
