from __future__ import annotations

import glob
import os
import re
from datetime import UTC, datetime

from flask import Blueprint, current_app, jsonify, request, send_file

from .. import maintenance
from ..errors import validation

bp = Blueprint("admin", __name__)


def llm_info(s) -> dict:
    from urllib.parse import urlparse

    host = urlparse(s.llm_base_url).hostname if s.llm_base_url else None
    local = None
    if host:
        local = (
            host in ("localhost", "host.docker.internal") or host.startswith(("127.", "10.", "192.168.")) or host.endswith(".local")
            or "." not in host
            or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host) is not None
            or re.match(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.", host) is not None
        )
    return {"configured": bool(s.llm_model), "model": s.llm_model or None, "host": host,
            "local": local}


def counts(conn) -> dict:
    out = {"notes": {"current": 0, "superseded": 0, "archived": 0}}
    for r in conn.execute("SELECT status, count(*) AS n FROM notes GROUP BY status"):
        out["notes"][r["status"]] = r["n"]
    out["entities"] = {"current": 0, "superseded": 0, "archived": 0}
    for r in conn.execute("SELECT status, count(*) AS n FROM entities GROUP BY status"):
        out["entities"][r["status"]] = r["n"]
    out["chunks"] = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    return out


@bp.get("/health")
def health():
    from .capture import _llm_info

    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return jsonify({
            "status": "needs_reindex" if hub.needs_reindex else "ok",
            "vector_backend": hub.store.backend,
            "embed_model": hub.embedder.model,
            "llm": llm_info(hub.settings),
            "capture": {
                "enabled": hub.settings.capture_enabled,
                **_llm_info(hub.settings),
            },
            "owner_configured": bool(conn.execute(
                "SELECT 1 FROM meta WHERE key='self_uid'"
            ).fetchone()),
            "counts": counts(conn),
        })
    finally:
        conn.close()


@bp.get("/api/stats")
def stats():
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return jsonify({**counts(conn), "vector_backend": hub.store.backend})
    finally:
        conn.close()


@bp.post("/api/admin/backup")
def backup():
    hub = current_app.extensions["hub"]
    d = os.path.join(hub.settings.data_dir, "backups")
    os.makedirs(d, exist_ok=True)
    name = f"knowledge-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.db"
    path = os.path.join(d, name)
    conn = hub.conn()  # isolation_level=None: VACUUM INTO cannot run in a transaction
    try:
        conn.execute("VACUUM INTO ?", (path,))
    finally:
        conn.close()
    files = sorted(glob.glob(os.path.join(d, "knowledge-*.db")))
    keep = hub.settings.backup_keep
    if keep < 1:
        raise validation("BACKUP_KEEP must be >= 1")
    for old in files[:-keep]:
        os.remove(old)
    return jsonify({"ok": True, "file": name}), 201


@bp.post("/api/admin/reindex")
def reindex():
    hub = current_app.extensions["hub"]
    return jsonify({"job": hub.reindexer.start(request.headers.get("X-Actor") or "ui")}), 202


@bp.get("/api/admin/reindex")
def reindex_status():
    hub = current_app.extensions["hub"]
    return jsonify({"job": hub.reindexer.status(), "needs_reindex": hub.needs_reindex})


@bp.post("/api/admin/mirror/rebuild")
def mirror_rebuild():
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return jsonify(maintenance.rebuild_mirror(hub, conn))
    finally:
        conn.close()


@bp.get("/api/admin/export")
def export():
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        f = maintenance.export_zip(hub, conn)
    finally:
        conn.close()
    name = f"knowledge-export-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    return send_file(f, mimetype="application/zip", as_attachment=True, download_name=name)


@bp.post("/api/import/scan")
def import_scan():
    hub = current_app.extensions["hub"]
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise validation("JSON object body required")
    pre = data.get("preprocess", False)
    if not isinstance(pre, bool):
        raise validation("preprocess must be a boolean")
    conn = hub.conn()
    try:
        return jsonify(maintenance.import_inbox(
            hub, conn, pre, (request.headers.get("X-Actor") or "ui")[:80]))
    finally:
        conn.close()
