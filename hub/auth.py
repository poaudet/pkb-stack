"""Authentication: one before_request hook for every route.

Modes (PKB_AUTH_MODE): token (bearer header or signed UI cookie), proxy (trust an
authenticating reverse proxy's header) or none. Only /health (data-free) and /login are public.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections import defaultdict, deque

from flask import Flask, Response, g, redirect, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .errors import ApiError

log = logging.getLogger(__name__)

MODES = ("token", "proxy", "none")
MIN_TOKEN_CHARS = 32
COOKIE = "pkb_session"
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILURES = 10
WINDOW_SECONDS = 60
PUBLIC_PATHS = frozenset({"/health", "/login", "/logout"})


class AuthConfigError(ValueError):
    """Raised at startup for an unusable authentication setup."""


def resolve_mode(mode: str, token: str, proxy_header: str) -> str:
    chosen = (mode or ("token" if token else "none")).strip().lower()
    if chosen not in MODES:
        raise AuthConfigError(f"PKB_AUTH_MODE must be one of {', '.join(MODES)}")
    if chosen == "token" and len(token) < MIN_TOKEN_CHARS:
        raise AuthConfigError(
            f"PKB_AUTH_MODE=token needs API_TOKEN of at least {MIN_TOKEN_CHARS} characters "
            "(generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\")")
    if chosen == "proxy" and not proxy_header.strip():
        raise AuthConfigError("PKB_AUTH_PROXY_HEADER must not be empty in proxy mode")
    return chosen


class _FailureLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._failures[key]
        while events and now - events[0] > WINDOW_SECONDS:
            events.popleft()
        if not events:
            self._failures.pop(key, None)
        return events

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, now)
            if len(events) >= MAX_FAILURES:
                return max(1, int(WINDOW_SECONDS - (now - events[0])) + 1)
        return 0

    def record(self, key: str) -> None:
        with self._lock:
            self._failures[key].append(time.monotonic())


def _serializer(token: str) -> URLSafeTimedSerializer:
    secret = hmac.new(token.encode(), b"pkb-session-cookie", hashlib.sha256).hexdigest()
    return URLSafeTimedSerializer(secret, salt="pkb-ui-session")


LOGIN_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>Sign in</title><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="font-family:system-ui;max-width:24rem;margin:4rem auto;padding:0 1rem">
<h1>Knowledge hub</h1><form method="post" action="/login">
<label>Access token <input name="token" type="password" autocomplete="current-password"
 required style="width:100%;margin:.5rem 0"></label>
<button type="submit">Sign in</button></form><p>{message}</p></body></html>"""


def init_auth(app: Flask, token: str, mode: str = "", proxy_header: str = "X-Forwarded-User",
              proxy_hops: int = 0) -> str:
    chosen = resolve_mode(mode, token, proxy_header)
    app.config["PKB_AUTH_MODE"] = chosen
    if chosen == "none":
        log.warning("auth mode is none: the API and UI are unauthenticated. Do not expose it.")
    limiter = _FailureLimiter()
    signer = _serializer(token) if chosen == "token" else None

    def client_key() -> str:
        # Behind N trusted proxies the Nth address from the right is the real client;
        # without it every client shares the proxy's address and one can lock out all.
        if proxy_hops > 0:
            hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",")
                    if h.strip()]
            if len(hops) >= proxy_hops:
                return hops[-proxy_hops]
        return request.remote_addr or "unknown"

    def token_matches(supplied: str) -> bool:
        return hmac.compare_digest(
            hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(token.encode()).digest())

    def has_valid_cookie() -> bool:
        value = request.cookies.get(COOKIE)
        if not value or signer is None:
            return False
        try:
            signer.loads(value, max_age=SESSION_SECONDS)
        except BadSignature:
            return False
        return True

    def throttle() -> None:
        wait = limiter.retry_after(client_key())
        if wait:
            raise ApiError("rate_limited", "too many failed attempts; try again later", 429,
                           retry_after=wait)

    def login_response(message: str, status: int) -> Response:
        return Response(LOGIN_PAGE.format(message=message), status=status,
                        mimetype="text/html")

    @app.before_request
    def _check():
        g.authenticated = chosen == "none"
        path = request.path
        if chosen == "none":
            return None
        if chosen == "proxy":
            g.authenticated = bool(request.headers.get(proxy_header, "").strip())
            if path in PUBLIC_PATHS and path != "/health":
                return redirect("/", 303)
            if path == "/health" or g.authenticated:
                return None
            raise ApiError("unauthorized", "authentication required", 401)

        if path == "/login":
            if request.method == "POST":
                throttle()
                if token_matches(request.form.get("token", "")):
                    response = redirect("/", 303)
                    response.set_cookie(
                        COOKIE, signer.dumps("ui"), max_age=SESSION_SECONDS, httponly=True,
                        samesite="Strict",
                        secure=request.is_secure
                        or request.headers.get("X-Forwarded-Proto") == "https")
                    return response
                limiter.record(client_key())
                return login_response("Invalid token.", 401)
            return login_response("", 200)
        if path == "/logout":
            response = redirect("/login", 303)
            response.delete_cookie(COOKIE, samesite="Strict")
            return response

        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if supplied:
            throttle()
            if token_matches(supplied):
                g.authenticated = True
            else:
                limiter.record(client_key())
                raise ApiError("unauthorized", "missing or invalid token", 401)
        elif has_valid_cookie():
            g.authenticated = True
        if g.authenticated or path == "/health":
            return None
        if path == "/" and request.method == "GET":
            return redirect("/login", 303)
        raise ApiError("unauthorized", "missing or invalid token", 401)

    return chosen
