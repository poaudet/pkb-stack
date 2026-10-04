from __future__ import annotations

import hmac

from flask import Flask, request

from .errors import ApiError


def init_auth(app: Flask, token: str) -> None:
    @app.before_request
    def _check() -> None:
        if not token or not request.path.startswith("/api/"):
            return
        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise ApiError("unauthorized", "missing or invalid token", 401)
