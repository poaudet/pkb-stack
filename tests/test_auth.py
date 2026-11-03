from __future__ import annotations

import re

import pytest

from hub.auth import COOKIE, AuthConfigError, resolve_mode

TOKEN = "k" * 40
BEARER = {"Authorization": f"Bearer {TOKEN}"}
PUBLIC = {"/health", "/login", "/logout"}


@pytest.fixture
def secured(mk):
    return mk(api_token=TOKEN).test_client()


def _concrete(rule) -> str:
    return re.sub(r"<(?:[a-z]+:)?[^>]+>", "x", rule.rule)


def test_every_route_requires_auth_except_public(secured):
    checked = 0
    for rule in secured.application.url_map.iter_rules():
        path = _concrete(rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            response = secured.open(path, method=method)
            if path in PUBLIC:
                assert response.status_code != 401, (method, path)
            else:
                assert response.status_code in (401, 303), (method, path, response.status_code)
                if response.status_code == 303:
                    assert path == "/"
            checked += 1
    assert checked > 40


def test_bearer_and_wrong_token(secured):
    assert secured.get("/api/stats", headers=BEARER).status_code == 200
    assert secured.get("/api/stats", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert secured.get("/static/app.js").status_code == 401
    assert secured.get("/static/app.js", headers=BEARER).status_code == 200


def test_health_is_data_free_without_auth(secured):
    body = secured.get("/health").get_json()
    assert set(body) == {"status"}
    assert "counts" in secured.get("/health", headers=BEARER).get_json()


def test_login_cookie_flow(secured):
    assert secured.get("/").headers["Location"].endswith("/login")
    assert secured.post("/login", data={"token": "bad"}).status_code == 401
    ok = secured.post("/login", data={"token": TOKEN})
    assert ok.status_code == 303
    cookie = ok.headers["Set-Cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert secured.get("/api/stats").status_code == 200
    assert secured.get("/").status_code == 200
    secured.get("/logout")
    assert secured.get("/api/stats").status_code == 401


def test_forged_cookie_is_rejected(secured):
    secured.set_cookie(COOKIE, "forged.value.here")
    assert secured.get("/api/stats").status_code == 401


def test_failed_attempts_are_rate_limited(secured):
    for _ in range(10):
        assert secured.get("/api/stats", headers={"Authorization": "Bearer x"}).status_code == 401
    limited = secured.get("/api/stats", headers={"Authorization": "Bearer x"})
    assert limited.status_code == 429
    assert limited.get_json()["error"]["code"] == "rate_limited"
    assert secured.get("/api/stats", headers=BEARER).status_code == 429


@pytest.mark.parametrize("args", [
    ("token", "short", "X"), ("bogus", TOKEN, "X"), ("token", "", "X"), ("proxy", "", " "),
])
def test_bad_auth_config_fails(args):
    with pytest.raises(AuthConfigError):
        resolve_mode(*args)


def test_bad_config_fails_startup(mk):
    with pytest.raises(AuthConfigError):
        mk(api_token="short", auth_mode="token")


def test_proxy_mode(mk):
    client = mk(auth_mode="proxy").test_client()
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-Forwarded-User": "me"}).status_code == 200
    assert client.get("/health").status_code == 200


def test_none_mode_is_open(mk):
    client = mk(auth_mode="none").test_client()
    assert client.get("/api/stats").status_code == 200
    assert "counts" in client.get("/health").get_json()
