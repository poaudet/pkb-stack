"""Personal Knowledge Hub. Importing this package has no side effects."""
from __future__ import annotations

import logging


def create_app(settings=None, embedder=None):
    from flask import Flask, jsonify
    from werkzeug.exceptions import HTTPException

    from .api import admin, entities, notes, pending, search, tools
    from .auth import init_auth
    from .config import Settings
    from .core import Hub
    from .errors import ApiError

    settings = settings or Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = settings.max_body_mb * 1024 * 1024
    hub = Hub(settings, embedder)
    app.extensions["hub"] = hub
    from .pending import Queue

    hub.queue = Queue(hub)
    hub.queue.recover()
    from .reindex import Reindexer

    hub.reindexer = Reindexer(hub)
    init_auth(app, settings.api_token)
    for m in (admin, entities, notes, pending, search, tools):
        app.register_blueprint(m.bp)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    def _err(code: str, message: str, status: int, **extra):
        return jsonify({"error": {"code": code, "message": message, **extra}}), status

    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        return _err(e.code, e.message, e.status, **e.extra)

    @app.errorhandler(404)
    def _404(_e):
        return _err("not_found", "not found", 404)

    @app.errorhandler(405)
    def _405(_e):
        return _err("validation_error", "method not allowed", 405)

    @app.errorhandler(413)
    def _413(_e):
        return _err("too_large", "request body too large", 413)

    @app.errorhandler(Exception)
    def _500(e: Exception):
        if isinstance(e, HTTPException):
            return _err("validation_error", e.description or e.name, e.code or 400)
        log = logging.getLogger(__name__)
        log.error("unhandled error: %s", type(e).__name__, exc_info=log.isEnabledFor(logging.DEBUG))
        return _err("internal", "internal error", 500)

    @app.after_request
    def _headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    return app
