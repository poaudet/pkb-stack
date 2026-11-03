from __future__ import annotations

import pytest

TOKEN = "t" * 40


def post(client, body, **kw):
    return client.post("/api/batch", json=body, **kw)


def count(client, type_name="person"):
    return len(client.get(f"/api/entities?type={type_name}&limit=200").get_json()["items"])


OPS = [
    {"op": "create_entity", "type": "person", "data": {"name": "B1", "birthday": "03-04"}},
    {"op": "create_note", "content": "batch note one", "tags": ["b"]},
    {"op": "create_entity", "type": "person", "data": {"name": "B2"}},
]


def test_atomic_success_creates_everything_and_is_searchable(client):
    r = post(client, {"ops": OPS})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["committed"]
    assert [x["ok"] for x in body["results"]] == [True, True, True]
    assert body["embeddings"] == {"pending": 0, "failed": 0}
    hits = client.get("/api/search?q=batch note one").get_json()
    assert "batch note one" in str(hits)


def test_atomic_failure_rolls_back_all(client):
    ops = [OPS[0], {"op": "create_entity", "type": "person", "data": {}}, OPS[1]]
    body = post(client, {"ops": ops}).get_json()
    assert not body["ok"] and not body["committed"]
    assert body["results"][1]["error"]["code"] == "validation_error"
    assert body["results"][2].get("skipped") is True
    assert count(client) == 0
    assert client.get("/api/notes").get_json()["items"] == []


def test_partial_keeps_good_ops(client):
    ops = [OPS[0], {"op": "create_entity", "type": "person", "data": {}}, OPS[1]]
    body = post(client, {"ops": ops, "mode": "partial"}).get_json()
    assert not body["ok"] and body["committed"]
    assert [x["ok"] for x in body["results"]] == [True, False, True]
    assert count(client) == 1 and len(client.get("/api/notes").get_json()["items"]) == 1


def test_dry_run_saves_nothing(client):
    body = post(client, {"ops": OPS, "dry_run": True}).get_json()
    assert body["ok"] and body["dry_run"] and not body["committed"]
    assert count(client) == 0 and client.get("/api/notes").get_json()["items"] == []


def test_update_supersede_archive(client):
    uid = post(client, {"ops": [OPS[0]]}).get_json()["results"][0]["result"]["entity"]["id"]
    body = post(client, {"ops": [
        {"op": "update_entity", "id": uid, "patch": {"relationship": "friend"}},
        {"op": "supersede_entity", "id": uid, "data": {"name": "B1"}, "reason": "redo"},
    ]}).get_json()
    assert body["ok"], body
    new = body["results"][1]["result"]["entity"]["id"]
    body = post(client, {"ops": [{"op": "archive_entity", "id": new}]}).get_json()
    assert body["results"][0]["result"]["entity"]["status"] == "archived"


@pytest.mark.parametrize("payload", [
    [], {}, {"ops": []}, {"ops": "x"}, {"ops": [1]}, {"ops": [OPS[0]], "mode": "x"},
    {"ops": [OPS[0]], "extra": 1}, {"ops": [OPS[0]], "dry_run": "yes"},
])
def test_malformed_envelope_is_400(client, payload):
    assert post(client, payload).status_code == 400


def test_unknown_op_is_reported_per_op(client):
    body = post(client, {"ops": [{"op": "nope"}], "mode": "partial"}).get_json()
    assert body["results"][0]["error"]["code"] == "validation_error"


def test_too_many_ops_is_413(mk):
    client = mk(batch_max_ops=2).test_client()
    r = post(client, {"ops": OPS})
    assert r.status_code == 413 and r.get_json()["error"]["code"] == "too_large"


def test_guard_blocks_secret_per_op_without_echo(client):
    secret = "sk-" + "a1" * 12
    body = post(client, {"ops": [
        {"op": "create_note", "content": f"key {secret}"}, OPS[0]], "mode": "partial"}).get_json()
    assert body["results"][0]["error"]["code"] == "secret_detected"
    assert secret not in str(body)
    assert body["results"][1]["ok"]


def test_embedding_failure_never_rolls_back(app, client, monkeypatch):
    hub = app.extensions["hub"]

    def boom(texts):
        raise RuntimeError("down")

    monkeypatch.setattr(hub.embedder, "embed_documents", boom)
    body = post(client, {"ops": [OPS[1]]}).get_json()
    assert body["ok"] and body["committed"] and body["embeddings"]["failed"] >= 1
    assert len(client.get("/api/notes").get_json()["items"]) == 1


def test_requires_token(mk):
    client = mk(api_token=TOKEN).test_client()
    assert post(client, {"ops": OPS}).status_code == 401
    ok = post(client, {"ops": OPS}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_tool_and_manifest(client):
    manifest = {t["function"]["name"] for t in client.get("/api/tools").get_json()}
    assert "batch" in manifest
    r = client.post("/api/tools/batch", json={"ops": [OPS[0], OPS[2]]}).get_json()
    assert r["ok"] and r["result"]["committed"]
    r = client.post("/api/tools/batch", json={"ops": [OPS[1]], "mode": "partial"}).get_json()
    assert r["result"]["results"][0]["error"]["code"] == "review_required"
