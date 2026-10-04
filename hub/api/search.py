from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import search as svc
from ..errors import validation

bp = Blueprint("search", __name__)


def _do(args: dict):
    hub = current_app.extensions["hub"]
    conn = hub.conn()
    try:
        return jsonify(svc.search(
            hub, conn, args.get("q"), limit=args.get("limit", 10), kinds=args.get("kinds"),
            tags=args.get("tags"), include_superseded=args.get("include_superseded", False),
            min_similarity=args.get("min_similarity"), since=args.get("since"),
            until=args.get("until")))
    except (TypeError, ValueError) as e:
        raise validation(str(e)) from e
    finally:
        conn.close()


@bp.post("/api/search")
def post_search():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise validation("JSON object body required")
    return _do(data)


@bp.get("/api/search")
def get_search():
    a = request.args
    return _do({
        "q": a.get("q"), "limit": a.get("limit", 10),
        "include_superseded": a.get("include_superseded") == "true",
        "kinds": a.getlist("kinds") or None, "tags": a.getlist("tags") or None,
    })
