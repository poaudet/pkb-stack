from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

from ..capture import apply as svc_apply
from ..capture import gates, jobs
from ..errors import validation
from .notes import _actor, _body, _page

bp = Blueprint("capture", __name__)


def _hub():
    return current_app.extensions["hub"]


def _with_conn(fn):
    conn = _hub().conn()
    try:
        return fn(conn)
    finally:
        conn.close()


def _llm_info(settings) -> dict:
    model = settings.capture_llm_model or settings.llm_model
    base_url = settings.capture_llm_base_url or settings.llm_base_url
    parsed = urlsplit(base_url) if base_url else None
    host = parsed.hostname if parsed else ("api.openai.com" if model else None)
    local = host in ("localhost", "host.docker.internal")
    if host:
        try:
            address = ipaddress.ip_address(host)
            carrier_nat = ipaddress.ip_network("100.64.0.0/10")
            local = (
                local or address.is_loopback or address.is_private or address in carrier_nat
            )
        except ValueError:
            local = local or host.endswith(".local") or "." not in host
    return {"model": model or None, "host": host, "local": bool(local)}


@bp.get("/api/capture/config")
def config():
    hub = _hub()
    return _with_conn(lambda conn: jsonify({
        "enabled": hub.settings.capture_enabled,
        **_llm_info(hub.llm_settings("capture")),
        "max_chars": hub.settings.capture_max_chars,
        "calls_today": gates.usage(conn),
        "daily_limit": hub.settings.capture_daily_call_limit,
        "resets_at": gates.resets_at(),
    }))


@bp.post("/api/capture")
def create():
    data = _body()
    return _with_conn(lambda conn: _created(jobs.create(_hub(), conn, data, _actor())))


def _created(result):
    payload, status = result
    return jsonify(payload), status


@bp.get("/api/capture")
def list_():
    limit, offset = _page()
    return _with_conn(lambda conn: jsonify(jobs.list_captures(
        conn, request.args.get("state"), request.args.get("source"), limit, offset)))


@bp.get("/api/capture/<uid>")
def get(uid: str):
    return _with_conn(lambda conn: jsonify({"capture": jobs.detail(conn, uid)}))


@bp.post("/api/capture/<uid>/regenerate")
def regenerate(uid: str):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise validation("JSON object body required")
    return _with_conn(lambda conn: (jsonify(
        {"capture": jobs.regenerate(_hub(), conn, uid, data)}), 202))


@bp.post("/api/capture/<uid>/reject")
def reject(uid: str):
    return _with_conn(lambda conn: jsonify(
        {"capture": jobs.reject(conn, uid, _actor())}))


@bp.post("/api/capture/<uid>/save-note")
def save_note(uid: str):
    return _with_conn(lambda conn: (jsonify(
        jobs.save_note(_hub(), conn, uid, _actor())), 201))


@bp.post("/api/capture/<uid>/apply")
def apply(uid: str):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise validation("JSON object body required")
    return _with_conn(lambda conn: (jsonify(
        svc_apply.apply(_hub(), conn, uid, data, _actor())), 200))


@bp.post("/api/capture/<uid>/validate")
def validate(uid: str):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise validation("JSON object body required")
    return _with_conn(lambda conn: jsonify(
        svc_apply.validate(_hub(), conn, uid, data)))


@bp.post("/api/capture/<uid>/undo")
def undo(uid: str):
    return _with_conn(lambda conn: jsonify(
        svc_apply.undo(_hub(), conn, uid, _actor())))
