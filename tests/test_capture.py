import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hub import create_app
from hub.capture import context, gates, plan
from hub.db import connect
from hub.errors import ApiError
from tests.conftest import make_settings

G1 = {
    "summary": "Working with Matt on PMT; launch Oct 13",
    "ops": [
        {
            "id": "o1", "op": "create_entity", "type": "person", "ref": "matt",
            "data": {"name": "Matt", "relationship": "collaborator"},
        },
        {
            "id": "o2", "op": "create_entity", "type": "project", "ref": "pmt",
            "data": {"title": "PMT", "status": "active", "start_date": "2026-06-01"},
        },
        {
            "id": "o3", "op": "create_entity", "type": "event",
            "data": {
                "date": "2026-10-13", "title": "PMT launch", "kind": "launch",
                "people": [{"$ref": "matt"}],
            },
        },
    ],
    "keep_note": False,
    "note": None,
    "questions": [],
}


class Stub:
    def __init__(self):
        self.reply = json.dumps(G1)
        self.replies = []
        self.reject_format = False
        self.calls = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.calls.append(request_body)
                if stub.reject_format and "response_format" in request_body:
                    return self.send_json(400, {"error": {"message": "unsupported"}})
                reply = stub.replies.pop(0) if stub.replies else stub.reply
                self.send_json(200, {
                    "id": "stub", "object": "chat.completion", "created": 0,
                    "model": "stub", "choices": [{
                        "index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": reply},
                    }],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
                })

            def send_json(self, status, value):
                content = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.close()


def make_capture_client(tmp_path, stub, **settings):
    app = create_app(make_settings(
        tmp_path, llm_model="capture-test", capture_llm_base_url=stub.url, **settings
    ))
    return app, app.test_client()


def wait_capture(client, uid, states=("ready", "failed"), timeout=10):
    until = time.time() + timeout
    while time.time() < until:
        response = client.get(f"/api/capture/{uid}")
        assert response.status_code == 200, response.get_json()
        capture = response.get_json()["capture"]
        if capture["state"] in states:
            return capture
        time.sleep(0.02)
    raise AssertionError("timed out waiting for capture")


def create_project_type(client):
    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string"},
            "status": {"type": "string"},
            "start_date": {"type": "string", "format": "date"},
        },
    }
    response = client.post("/api/entity-types", json={
        "name": "project", "json_schema": schema, "unique_key": "title",
        "text_template": "{title}",
    })
    assert response.status_code == 201, response.get_json()


def submit(client, text="Working with Matt on PMT since June, launch is Oct 13.", **data):
    response = client.post("/api/capture", json={"text": text, **data})
    assert response.status_code == 202, response.get_json()
    return response.get_json()["capture"]["id"]


def test_fold_and_capture_schema_migration(tmp_path):
    conn = connect(str(tmp_path / "fold.db"), load_vec=False)
    try:
        assert conn.execute("SELECT fold(?)", ("ÉCOLE",)).fetchone()[0] == "ecole"
        assert conn.execute("SELECT fold(?)", ("Straße",)).fetchone()[0] == "strasse"
    finally:
        conn.close()

    app = create_app(make_settings(tmp_path))
    conn = app.extensions["hub"].conn()
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"captures", "capture_ops", "capture_usage"} <= tables
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "7"
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert "idx_captures_hash_state" in indexes
    finally:
        conn.close()


def test_capture_not_configured_and_health_redacts_keys(client):
    response = client.post("/api/capture", json={"text": "remember this"})
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "llm_not_configured"
    health = client.get("/health").get_json()["capture"]
    assert health == {"enabled": True, "model": None, "host": None, "local": False}
    assert "api_key" not in json.dumps(health)


def test_gates_detect_luhn_credentials_and_limit(app):
    hub = app.extensions["hub"]
    conn = hub.conn()
    try:
        assert gates.secret_kind("card 4111111111111111") == "payment_card"
        assert gates.secret_kind("password: hunter2!") == "credential"
        with pytest.raises(ApiError) as caught:
            gates.gate(conn, hub.settings, "card 4111111111111111")
        assert caught.value.code == "secret_detected"
        conn.execute(
            "INSERT INTO capture_usage(day,calls) VALUES(?,?)",
            (gates.current_day(), 60),
        )
        with pytest.raises(ApiError) as caught:
            gates.gate(conn, hub.settings, "new detail")
        assert caught.value.code == "rate_limited"
        assert "resets_at" in caught.value.extra
    finally:
        conn.close()


def test_capture_catalog_and_folded_candidates(client, app):
    client.post("/api/entities", json={"type": "person", "data": {"name": "Élodie"}})
    conn = app.extensions["hub"].conn()
    try:
        catalog = context.build_catalog(conn)
        assert "person —" in catalog and "memory —" in catalog
        assert "place_visit" not in catalog
        candidates = context.build_candidates(
            app.extensions["hub"], conn, "I met Elodie again.", limit=10
        )
        assert candidates[0]["label"] == "Élodie"
        assert "Élodie" not in context.candidate_lines([])
    finally:
        conn.close()


def test_pipeline_g1_apply_refs_and_undo(tmp_path, stub):
    app, client = make_capture_client(tmp_path, stub)
    create_project_type(client)
    uid = submit(client, as_of="2026-10-06")
    capture = wait_capture(client, uid)
    assert capture["state"] == "ready"
    assert [op["status"] for op in capture["plan"]["ops"]] == ["valid"] * 3
    assert "tools" not in stub.calls[0] and "tool_choice" not in stub.calls[0]
    assert capture["prompt_tokens"] == 12 and capture["completion_tokens"] == 7

    response = client.post(f"/api/capture/{uid}/apply", json={})
    assert response.status_code == 200, response.get_json()
    applied = response.get_json()["applied"]
    assert len(applied) == 3
    event = client.get(f"/api/entities/{applied[2]['id']}").get_json()["entity"]
    person = client.get(f"/api/entities/{applied[0]['id']}").get_json()["entity"]
    assert event["data"]["people"] == [person["id"]]
    conn = app.extensions["hub"].conn()
    try:
        assert conn.execute(
            "SELECT count(*) FROM audit_log WHERE action='capture_apply'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    response = client.post(f"/api/capture/{uid}/undo", json={})
    assert response.status_code == 200, response.get_json()
    assert len(response.get_json()["undone"]) == 3
    assert client.get(f"/api/entities/{person['id']}").get_json()["entity"]["status"] == "archived"


def test_french_values_and_iso_dates_survive_normalization(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    stub.reply = json.dumps({
        "summary": "Une chatte, Tequila, est née le 24 juin 2010.",
        "ops": [{
            "id": "p1", "op": "create_entity", "type": "person",
            "data": {"name": "Tequila", "relationship": "chatte", "birthday": "2010-06-24"},
        }],
        "keep_note": False, "questions": [],
    }, ensure_ascii=False)
    uid = submit(client, "J'avais une chatte Tequila née le 24 juin 2010.", as_of="2026-10-06")
    capture = wait_capture(client, uid)
    assert capture["state"] == "ready"
    applied = client.post(f"/api/capture/{uid}/apply", json={})
    assert applied.status_code == 200, applied.get_json()
    person = client.get(
        f"/api/entities/{applied.get_json()['applied'][0]['id']}"
    ).get_json()["entity"]
    assert person["data"]["relationship"] == "chatte"
    assert person["data"]["birthday"] == "2010-06-24"


def test_json_mode_fallback_and_single_repair(tmp_path, stub):
    stub.reject_format = True
    stub.replies = ["not JSON", json.dumps(G1)]
    _app, client = make_capture_client(tmp_path, stub)
    uid = submit(client)
    capture = wait_capture(client, uid)
    assert capture["state"] == "ready"
    assert len(stub.calls) == 4
    assert "response_format" in stub.calls[0]
    assert "response_format" not in stub.calls[1]
    assert "response_format" in stub.calls[2]
    assert "response_format" not in stub.calls[3]
    assert "Return the corrected JSON object only" in stub.calls[3]["messages"][-1]["content"]
    config = client.get("/api/capture/config").get_json()
    assert config["calls_today"] == 4 and config["local"] is True


def test_capture_gates_duplicate_save_note_and_skip_ai(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub, capture_max_chars=20)
    too_long = client.post("/api/capture", json={"text": "x" * 21})
    assert too_long.status_code == 400 and too_long.get_json()["error"]["code"] == "too_long"
    secret = client.post("/api/capture", json={"text": "password: hunter2!"})
    assert secret.status_code == 409 and secret.get_json()["error"]["kind"] == "credential"

    response = client.post("/api/capture", json={"text": "remember this", "skip_ai": True})
    assert response.status_code == 201
    assert response.get_json()["note"]["source"] == "capture"
    uid = submit(client, "another capture")
    wait_capture(client, uid)
    note = client.post(f"/api/capture/{uid}/save-note", json={})
    assert note.status_code == 201
    assert note.get_json()["note"]["content"] == "another capture"
    assert note.get_json()["capture"]["state"] == "rejected"


def test_daily_limit_and_duplicate_after_apply(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub, capture_daily_call_limit=1)
    uid = submit(client)
    capture = wait_capture(client, uid)
    assert capture["state"] == "ready"
    assert client.post(f"/api/capture/{uid}/apply", json={}).status_code == 200
    duplicate = client.post("/api/capture", json={"text": "Working with Matt on PMT since June, launch is Oct 13."})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"]["code"] == "duplicate"
    limited = client.post("/api/capture", json={"text": "not a duplicate"})
    assert limited.status_code == 429
    assert limited.get_json()["error"]["code"] == "rate_limited"


def test_capture_queue_recovers_processing_state(tmp_path, stub):
    app, client = make_capture_client(tmp_path, stub)
    conn = app.extensions["hub"].conn()
    try:
        conn.execute(
            "INSERT INTO captures(uid,raw_text,text_hash,as_of,state) "
            "VALUES('recover','recover this','hash','2026-10-09','processing')"
        )
    finally:
        conn.close()
    app.extensions["hub"].capture_queue.recover()
    capture = wait_capture(client, "recover")
    assert capture["state"] == "ready"


def test_plan_validation_injection_conversion_and_overwrite(client, app):
    existing = client.post("/api/entities", json={
        "type": "person", "data": {"name": "Alice", "notes": "French text"},
    }).get_json()["entity"]
    conn = app.extensions["hub"].conn()
    try:
        candidates = [{"id": existing["id"], "type": "person", "label": "Alice"}]
        converted = plan.normalize(conn, {
            "ops": [{"id": "bad", "op": "create_entity", "type": "person",
                     "data": {"name": "alice", "unknown": "discard"}}],
        }, "Alice", candidates)
        assert converted["ops"][0]["status"] == "converted"
        assert converted["ops"][0]["target"] == existing["id"]
        assert "dropped unknown field: unknown" in converted["ops"][0]["warnings"]
        overwritten = plan.normalize(conn, {
            "ops": [{"id": "u", "op": "update_entity", "target": existing["id"],
                     "patch": {"notes": "Réécrit"}}],
        }, "Alice", candidates)
        op = overwritten["ops"][0]
        assert op["include"] is False
        assert any(warning.startswith("overwrites_text:") for warning in op["warnings"])
        malicious = plan.normalize(conn, {
            "ops": [{"id": "x", "op": "archive_entity", "target": existing["id"]},
                    {"id": "y", "op": "create_entity", "type": "event",
                     "data": {"date": "2026-01-01", "title": "x",
                              "people": [{"$ref": "missing"}]}}],
        }, "x", candidates)
        assert len(malicious["ops"]) == 1
        assert malicious["warnings"][0].startswith("dropped unsupported operation")
        assert malicious["ops"][0]["status"] == "invalid"
        missing = plan.normalize(conn, {
            "ops": [{"id": "missing", "op": "create_entity", "type": "person",
                     "data": {"relationship": "friend"}}],
            "questions": ["What is the person's name?"],
        }, "A friend", candidates)
        assert missing["ops"][0]["status"] == "invalid"
        assert "missing: name" in missing["ops"][0]["errors"][0]
        assert missing["questions"] == ["What is the person's name?"]
    finally:
        conn.close()


def test_apply_override_revalidation_and_atomic_rollback(tmp_path, stub):
    app, client = make_capture_client(tmp_path, stub)
    stub.reply = json.dumps({
        "ops": [
            {"id": "o1", "op": "create_entity", "type": "person",
             "data": {"name": "New Person"}},
        ],
        "keep_note": True,
        "note": {"title": "Keep", "tags": []},
        "questions": [],
    })
    raw = "the exact capture text"
    client.post("/api/notes", json={"content": raw})
    uid = submit(client, raw)
    assert wait_capture(client, uid)["state"] == "ready"
    response = client.post(f"/api/capture/{uid}/apply", json={
        "ops": [{"id": "o1", "data": {"name": 7}}],
    })
    assert response.status_code == 400, response.get_json()
    assert client.get("/api/capture/" + uid).get_json()["capture"]["state"] == "ready"
    response = client.post(f"/api/capture/{uid}/apply", json={})
    assert response.status_code == 409
    conn = app.extensions["hub"].conn()
    try:
        assert conn.execute(
            "SELECT count(*) FROM entities WHERE type='person' AND status='current'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_update_undo_keeps_modified_entity(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    person = client.post("/api/entities", json={
        "type": "person", "data": {"name": "Alice", "interests": ["reading"]},
    }).get_json()["entity"]
    stub.reply = json.dumps({
        "ops": [{"id": "u1", "op": "update_entity", "target": person["id"],
                 "patch": {"interests": ["hiking", "READING"]}}],
        "keep_note": False,
        "questions": [],
    })
    uid = submit(client, "Alice likes hiking.")
    capture = wait_capture(client, uid)
    assert capture["plan"]["ops"][0]["status"] == "valid"
    applied = client.post(f"/api/capture/{uid}/apply", json={})
    assert applied.status_code == 200
    after = client.get(f"/api/entities/{person['id']}").get_json()["entity"]
    assert after["data"]["interests"] == ["reading", "hiking"]
    client.patch(f"/api/entities/{person['id']}", json={"notes": "Later edit"})
    undone = client.post(f"/api/capture/{uid}/undo", json={})
    assert undone.status_code == 200
    assert undone.get_json()["kept_modified"] == ["u1"]
    final = client.get(f"/api/entities/{person['id']}").get_json()["entity"]
    assert final["data"]["notes"] == "Later edit"


def test_apply_unticked_operation_and_supersede_undo(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    person = client.post("/api/entities", json={
        "type": "person", "data": {"name": "Old", "relationship": "friend"},
    }).get_json()["entity"]
    stub.reply = json.dumps({
        "ops": [{
            "id": "s1", "op": "supersede_entity", "target": person["id"],
            "data": {"name": "Old", "relationship": "colleague"},
            "reason": "relationship changed",
        }],
        "keep_note": False,
        "questions": [],
    })
    uid = submit(client, "Old is now a colleague.")
    capture = wait_capture(client, uid)
    assert capture["plan"]["ops"][0]["status"] == "valid"
    skipped = client.post(f"/api/capture/{uid}/apply", json={
        "ops": [{"id": "s1", "include": False}],
    })
    assert skipped.status_code == 200 and skipped.get_json()["applied"] == []
    assert skipped.get_json()["skipped"] == ["s1"]
    assert client.get(f"/api/entities/{person['id']}").get_json()["entity"]["status"] == "current"

    # Re-create and apply the plan on a new capture to exercise supersede undo.
    stub.reply = json.dumps({
        "ops": [{
            "id": "s2", "op": "supersede_entity", "target": person["id"],
            "data": {"name": "Old", "relationship": "colleague"},
            "reason": "relationship changed",
        }],
        "keep_note": False,
        "questions": [],
    })
    second = submit(client, "Old is now a colleague.", allow_duplicate=True)
    assert wait_capture(client, second)["state"] == "ready"
    applied = client.post(f"/api/capture/{second}/apply", json={})
    assert applied.status_code == 200, applied.get_json()
    replacement = applied.get_json()["applied"][0]["id"]
    assert client.get(f"/api/entities/{person['id']}").get_json()["entity"]["status"] == "superseded"
    undone = client.post(f"/api/capture/{second}/undo", json={})
    assert undone.status_code == 200
    assert client.get(f"/api/entities/{person['id']}").get_json()["entity"]["status"] == "current"
    assert client.get(f"/api/entities/{replacement}").get_json()["entity"]["status"] == "archived"


def test_new_type_application_requires_explicit_opt_in_and_is_left_by_undo(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    stub.reply = json.dumps({
        "ops": [
            {
                "id": "t1", "op": "create_entity_type", "name": "book",
                "description": "Books",
                "json_schema": {
                    "type": "object", "required": ["title"],
                    "properties": {"title": {"type": "string"}},
                },
                "unique_key": "title", "embed": False,
            },
            {"id": "e1", "op": "create_entity", "type": "book",
             "data": {"title": "Dune"}},
        ],
        "keep_note": False,
        "questions": [],
    })
    uid = submit(client, "Read Dune.")
    capture = wait_capture(client, uid)
    assert [op["kind"] for op in capture["plan"]["ops"]] == [
        "create_entity_type", "create_entity",
    ]
    refused = client.post(f"/api/capture/{uid}/apply", json={
        "ops": [{"id": "t1", "include": True}],
    })
    assert refused.status_code == 400
    applied = client.post(f"/api/capture/{uid}/apply", json={
        "ops": [{"id": "t1", "include": True}],
        "allow_new_types": True,
    })
    assert applied.status_code == 200, applied.get_json()
    undone = client.post(f"/api/capture/{uid}/undo", json={})
    assert undone.status_code == 200
    assert undone.get_json()["types_left"] == ["book"]
    assert client.get("/api/entity-types/book").status_code == 200
