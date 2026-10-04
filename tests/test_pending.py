import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hub import create_app
from tests.conftest import make_settings


class Stub:
    """Minimal OpenAI-compatible chat completions server."""

    def __init__(self):
        self.reply = lambda body: json.dumps({
            "title": "Tidy", "content": "Tidied " + "text", "tags": ["A"], "happened_at": None})
        self.reject_format = False
        self.fail = False
        self.calls = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.calls.append(body)
                if stub.fail:
                    return self._send(500, {"error": {"message": "boom"}})
                if stub.reject_format and "response_format" in body:
                    return self._send(400, {"error": {"message": "unsupported"}})
                self._send(200, {
                    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": stub.reply(body)}}]})

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.srv.shutdown()


@pytest.fixture
def llm_client(tmp_path, stub):
    app = create_app(make_settings(tmp_path, llm_base_url=stub.url, llm_model="m"))
    return app.test_client()


def wait_state(c, uid, states, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        p = c.get(f"/api/pending/{uid}").get_json()["pending"]
        if p["state"] in states:
            return p
        time.sleep(0.05)
    raise AssertionError("timeout")


def submit(c, content="Alice paid 42 EUR on 2024-05-01", **kw):
    r = c.post("/api/notes", json={"content": content, "preprocess": True, **kw})
    assert r.status_code == 202, r.get_json()
    return r.get_json()["pending"]["id"]


def test_preprocess_requires_llm(client):
    r = client.post("/api/notes", json={"content": "x", "preprocess": True})
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "llm_not_configured"


def test_success_and_approve(llm_client):
    uid = submit(llm_client)
    p = wait_state(llm_client, uid, ("ready", "failed"))
    assert p["state"] == "ready" and p["suggestion"]["tags"] == ["a"]
    assert "Alice" in p["warnings"]["missing"] and "42" in p["warnings"]["missing"]
    assert p["diff"]
    r = llm_client.post(f"/api/pending/{uid}/approve", json={})
    assert r.status_code == 201
    note = r.get_json()["note"]
    assert note["raw_content"] == "Alice paid 42 EUR on 2024-05-01"
    assert llm_client.get(f"/api/pending/{uid}").get_json()["pending"]["state"] == "approved"
    assert llm_client.post(f"/api/pending/{uid}/approve", json={}).status_code == 409


def test_failure_keeps_raw_and_approvable(llm_client, stub):
    stub.fail = True
    uid = submit(llm_client)
    p = wait_state(llm_client, uid, ("failed",))
    assert p["suggestion"]["content"] == p["raw_content"] and p["error"]
    r = llm_client.post(f"/api/pending/{uid}/approve", json={})
    assert r.status_code == 201 and r.get_json()["note"]["content"] == p["raw_content"]


def test_response_format_retry_and_fences(llm_client, stub):
    stub.reject_format = True
    stub.reply = lambda b: "```json\n" + json.dumps(
        {"title": "T", "content": "C", "tags": [], "happened_at": "2024-01-02"}) + "\n```"
    p = wait_state(llm_client, submit(llm_client), ("ready", "failed"))
    assert p["state"] == "ready" and p["suggestion"]["happened_at"] == "2024-01-02"
    assert "response_format" in stub.calls[0] and "response_format" not in stub.calls[-1]


def test_invalid_json_retries_then_fails(llm_client, stub):
    stub.reply = lambda b: "not json"
    p = wait_state(llm_client, submit(llm_client), ("ready", "failed"))
    assert p["state"] == "failed" and len(stub.calls) == 2


def test_too_long(tmp_path, stub):
    c = create_app(make_settings(tmp_path, llm_base_url=stub.url, llm_model="m",
                                 llm_max_input_chars=10)).test_client()
    p = wait_state(c, submit(c, "x" * 50), ("failed",))
    assert p["error"] == "too_long" and not stub.calls


def test_approve_with_supersede(llm_client):
    old = llm_client.post("/api/notes", json={"content": "old fact"}).get_json()["note"]
    uid = submit(llm_client)
    wait_state(llm_client, uid, ("ready",))
    r = llm_client.post(f"/api/pending/{uid}/approve", json={"supersede": [old["id"]]})
    assert r.status_code == 201
    got = llm_client.get(f"/api/notes/{old['id']}").get_json()["note"]
    assert got["status"] == "superseded" and got["superseded_by"] == r.get_json()["note"]["id"]


def test_reject_and_regenerate(llm_client):
    uid = submit(llm_client)
    wait_state(llm_client, uid, ("ready",))
    assert llm_client.post(f"/api/pending/{uid}/regenerate").status_code == 202
    wait_state(llm_client, uid, ("ready",))
    assert llm_client.post(f"/api/pending/{uid}/reject").status_code == 200
    assert llm_client.post(f"/api/pending/{uid}/reject").status_code == 409


def test_restart_recovery(tmp_path, stub):
    s = make_settings(tmp_path, llm_base_url=stub.url, llm_model="m")
    app = create_app(s)
    hub = app.extensions["hub"]
    conn = hub.conn()
    conn.execute("INSERT INTO pending_notes(uid,raw_content,state) VALUES('abc','hello Bob','processing')")
    conn.close()
    c = create_app(s).test_client()
    assert wait_state(c, "abc", ("ready",))["state"] == "ready"


def test_agent_note_goes_to_review(client):
    r = client.post("/api/notes", json={"content": "agent says hi"},
                    headers={"X-Actor": "agent:bot"})
    assert r.status_code == 202
    p = r.get_json()["pending"]
    assert p["state"] == "ready" and p["source"] == "agent:bot"
    assert client.get("/api/notes").get_json()["total"] == 0
    assert client.post(f"/api/pending/{p['id']}/approve", json={}).status_code == 201


def test_patch_pending(llm_client, stub):
    stub.fail = True
    uid = wait_state(llm_client, submit(llm_client), ("failed",))["id"]
    r = llm_client.patch(f"/api/pending/{uid}", json={"title": "Mine"})
    assert r.get_json()["pending"]["suggestion"]["title"] == "Mine"
