from __future__ import annotations

import json
import re

import pytest

from hub import db
from hub.capture import context, plan
from tests.test_capture import Stub, make_capture_client, submit, wait_capture


@pytest.fixture
def capture_stub():
    stub = Stub()
    yield stub
    stub.close()


def _type(client, name, *, policy=None, properties=None):
    return client.post("/api/entity-types", json={
        "name": name,
        "json_schema": {
            "type": "object", "properties": properties or {"title": {"type": "string"}},
        },
        **({"policy": policy} if policy is not None else {}),
    })


def _tool(client, name, args):
    return client.post(f"/api/tools/{name}", json=args).get_json()


def test_client_uid_health_and_optimistic_concurrency(client, app):
    uid = "a" * 32
    created = client.post("/api/entities", json={
        "uid": uid, "type": "person", "data": {"name": "UID Person"},
    })
    assert created.status_code == 201
    assert created.get_json()["entity"]["id"] == uid
    assert client.get("/health").get_json()["features"] == ["client_uid"]
    assert client.post("/api/entities", json={
        "uid": "bad", "type": "person", "data": {"name": "Invalid"},
    }).status_code == 400
    assert client.post("/api/entities", json={
        "uid": uid, "type": "person", "data": {"name": "Duplicate"},
    }).status_code == 409
    unchanged = client.post("/api/entities", json={
        "type": "person", "data": {"name": "No client uid"},
    }).get_json()["entity"]
    assert re.fullmatch(r"[0-9a-f]{32}", unchanged["id"])

    entity = created.get_json()["entity"]
    stale = client.patch(f"/api/entities/{uid}", json={"notes": "Changed"})
    assert stale.status_code == 200
    conn = app.extensions["hub"].conn()
    try:
        conn.execute(
            "UPDATE entities SET updated_at='2026-10-19T11:00:00Z' WHERE uid=?", (uid,)
        )
    finally:
        conn.close()
    stale = client.patch(
        f"/api/entities/{uid}", json={"notes": "Old edit"},
        headers={"X-Expected-Updated-At": entity["updated_at"]},
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "stale"
    assert stale.get_json()["error"]["current"]["data"]["notes"] == "Changed"
    assert client.patch(f"/api/entities/{uid}", json={"notes": "Compatible"}).status_code == 200
    assert client.post(f"/api/entities/{uid}/archive", json={}).status_code == 200
    assert client.post("/api/entities", json={
        "uid": uid, "type": "person", "data": {"name": "Duplicate archived"},
    }).status_code == 409


def test_archived_entity_search_and_counts_are_opt_in(client):
    entity = client.post("/api/entities", json={
        "type": "event",
        "data": {"date": "2026-10-18", "title": "Archived search marker"},
    }).get_json()["entity"]
    assert client.post(f"/api/entities/{entity['id']}/archive", json={}).status_code == 200
    assert client.get("/api/entities?type=event&status=current").get_json()["total"] == 0
    assert client.get("/api/entities?type=event&status=current,archived").get_json()["total"] == 1
    counts = next(
        item["counts"] for item in client.get("/api/entity-types").get_json()["types"]
        if item["name"] == "event"
    )
    assert counts == {"current": 0, "archived": 1, "superseded": 0}
    hidden = client.post("/api/search", json={"q": "Archived search marker"}).get_json()["hits"]
    visible = client.post("/api/search", json={
        "q": "Archived search marker", "include_archived": True,
    }).get_json()["hits"]
    assert all(hit["id"] != entity["id"] for hit in hidden)
    assert any(hit["id"] == entity["id"] and hit["status"] == "archived" for hit in visible)


def test_searchable_policy_hides_existing_entity_chunks(client):
    assert _type(client, "searchable_record").status_code == 201
    entity = client.post("/api/entities", json={
        "type": "searchable_record", "data": {"title": "Search policy marker"},
    }).get_json()["entity"]
    found = client.post("/api/search", json={"q": "Search policy marker"}).get_json()["hits"]
    assert any(hit["id"] == entity["id"] for hit in found)
    assert client.patch("/api/entity-types/searchable_record", json={
        "policy": {"searchable": False},
    }).status_code == 200
    hidden = client.post("/api/search", json={"q": "Search policy marker"}).get_json()["hits"]
    assert all(hit["id"] != entity["id"] for hit in hidden)


def test_capture_exclusion_covers_catalog_candidates_normalize_and_apply(tmp_path, capture_stub):
    stub = capture_stub
    app, client = make_capture_client(tmp_path, stub, capture_exclude_types="task")
    assert _type(client, "task", properties={"title": {"type": "string"}}).status_code == 201
    entity = client.post("/api/entities", json={
        "type": "task", "data": {"title": "Buy milk"},
    }).get_json()["entity"]
    stub.reply = json.dumps({
        "summary": "Pick up milk",
        "ops": [
            {"id": "u1", "op": "update_entity", "target": entity["id"],
             "patch": {"title": "Groceries"}},
            {"id": "c1", "op": "create_entity", "type": "task",
             "data": {"title": "Buy bread"}},
        ],
        "keep_note": False, "questions": [],
    })
    uid = submit(client, "I need to pick up some milk before dinner.")
    capture = wait_capture(client, uid)
    assert capture["state"] == "ready"
    assert capture["plan"]["ops"] == []
    assert "type_excluded" in capture["plan"]["warnings"]
    prompt = json.dumps(stub.calls[0], ensure_ascii=False)
    assert "Buy milk" not in prompt
    assert entity["id"] not in prompt

    conn = app.extensions["hub"].conn()
    try:
        assert not any(
            line.startswith("task —") for line in context.build_catalog(conn, "task").splitlines()
        )
        assert context.build_candidates(
            app.extensions["hub"], conn, "milk", 10, "task"
        ) == []
        altered = {"ops": [{
            "id": "manual", "op": "update_entity", "target": entity["id"],
            "patch": {"title": "Changed"},
        }]}
        row = conn.execute("SELECT id FROM captures WHERE uid=?", (uid,)).fetchone()
        conn.execute("UPDATE captures SET plan=? WHERE id=?",
                     (json.dumps(altered), row["id"]))
    finally:
        conn.close()
    rejected = client.post(f"/api/capture/{uid}/apply", json={})
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "type_excluded"


def test_extend_entity_type_tool_dry_run_confirm_idempotence_and_gate(mk):
    client = mk().test_client()
    assert _type(client, "book").status_code == 201
    before = client.get("/api/entity-types/book").get_json()["type"]
    args = {"name": "book", "add_properties": {
        "author": {"type": "string", "description": "Book author"},
    }}
    preview = _tool(client, "extend_entity_type", args)
    assert preview["ok"] and preview["result"]["applied"] is False
    assert preview["result"]["diff"][0]["name"] == "author"
    assert client.get("/api/entity-types/book").get_json()["type"]["schema_version"] == before[
        "schema_version"
    ]

    applied = _tool(client, "extend_entity_type", {**args, "confirm": True})
    assert applied["ok"] and applied["result"]["applied"] is True
    type_info = applied["result"]["type"]
    assert type_info["json_schema"]["properties"]["author"]["x-origin"] == "agent:api"
    assert client.get("/api/entity-types/book/history").get_json()["history"][-1]["actor"] == "agent:api"
    assert _tool(client, "extend_entity_type", {**args, "confirm": True})["result"] == {
        "applied": False, "reason": "exists", "schema_version": type_info["schema_version"],
    }
    conflict = _tool(client, "extend_entity_type", {
        **args, "add_properties": {"author": {"type": "string", "description": "Different"}},
    })
    assert conflict["error"]["code"] == "conflict"
    manifest = {tool["function"]["name"] for tool in client.get("/api/tools").get_json()}
    assert "extend_entity_type" in manifest

    disabled = mk(tools_allow_extend=False).test_client()
    assert _type(disabled, "locked_book").status_code == 201
    refusal = _tool(disabled, "extend_entity_type", {**args, "name": "locked_book"})
    assert refusal["error"]["code"] == "forbidden"


def test_extension_policy_locks_type_definition_and_all_extension_paths(client, app):
    created = _type(client, "fixed_schema", policy={"allow_extension": False})
    assert created.status_code == 201
    type_info = created.get_json()["type"]
    assert type_info["policy"]["allow_extension"] is False

    extension = {
        "add_properties": {"author": {"type": "string", "description": "Book author"}},
        "confirm": True,
    }
    response = client.post("/api/entity-types/fixed_schema/extend", json=extension)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "forbidden"

    tool = _tool(client, "extend_entity_type", {
        "name": "fixed_schema", "add_properties": extension["add_properties"],
    })
    assert tool["error"]["code"] == "forbidden"

    tool_created = _tool(client, "create_entity_type", {
        "name": "agent_fixed_schema",
        "json_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
        "policy": {"allow_extension": False},
    })
    assert tool_created["ok"]
    assert tool_created["result"]["type"]["policy"]["allow_extension"] is False

    conn = app.extensions["hub"].conn()
    try:
        normalized = plan.normalize(
            conn,
            {"ops": [{
                "id": "extension", "op": "extend_entity_type", "type": "fixed_schema",
                "add_properties": extension["add_properties"],
            }]},
            "remember this",
            [],
        )
        assert normalized["ops"][0]["status"] == "invalid"
        assert "schema extension is disabled" in normalized["ops"][0]["errors"][0]
    finally:
        conn.close()

    patched = client.patch("/api/entity-types/fixed_schema", json={
        "policy": {"allow_extension": True},
    })
    assert patched.status_code == 200
    assert patched.get_json()["type"]["policy"]["allow_extension"] is True
    assert client.post("/api/entity-types/fixed_schema/extend", json=extension).status_code == 200


def test_type_delete_refusals_cascade_and_convert_entities_to_notes(client, app):
    assert _type(client, "custom").status_code == 201
    entity = client.post("/api/entities", json={
        "type": "custom", "data": {"title": "Important detail"},
    }).get_json()["entity"]
    assert client.delete("/api/entity-types/custom").status_code == 400
    blocked = client.delete("/api/entity-types/custom?confirm=true")
    assert blocked.status_code == 409
    assert blocked.get_json()["error"]["reasons"][0]["kind"] == "entities"

    dependent = client.post("/api/entity-types", json={
        "name": "referrer",
        "json_schema": {"type": "object", "properties": {
            "target": {"type": "string", "x-ref": "custom"},
        }},
    })
    assert dependent.status_code == 201
    ref_block = client.delete("/api/entity-types/custom?confirm=true&cascade=true")
    assert ref_block.status_code == 409
    assert ref_block.get_json()["error"]["referenced_by"] == ["referrer"]
    assert client.delete("/api/entity-types/person?confirm=true&cascade=true").status_code == 409
    client.delete("/api/entity-types/referrer?confirm=true")

    conn = app.extensions["hub"].conn()
    try:
        before_notes = conn.execute("SELECT count(*) FROM notes").fetchone()[0]
        audit_before = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        chunk_id = conn.execute(
            "SELECT c.id FROM chunks c JOIN entities e ON e.id=c.owner_id "
            "WHERE c.owner_kind='entity' AND e.uid=?", (entity["id"],)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO captures(uid,text_hash,state) VALUES(?,?,?)",
            ("running-capture", "hash", "processing"),
        )
    finally:
        conn.close()
    running = client.delete("/api/entity-types/custom?confirm=true&cascade=true")
    assert running.status_code == 409
    assert "capture_running" in {
        reason["kind"] for reason in running.get_json()["error"]["reasons"]
    }
    conn = app.extensions["hub"].conn()
    conn.execute("UPDATE captures SET state='ready' WHERE uid='running-capture'")
    conn.close()
    deleted = client.delete(
        "/api/entity-types/custom?confirm=true&cascade=true&convert_to_notes=true"
    )
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()["converted_to_notes"] == 1
    assert deleted.get_json()["counts"] == {"current": 1, "superseded": 0, "archived": 0}
    assert client.delete("/api/entity-types/custom?confirm=true").status_code == 404
    conn = app.extensions["hub"].conn()
    try:
        assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == before_notes + 1
        assert conn.execute("SELECT count(*) FROM entities WHERE uid=?", (entity["id"],)
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chunks WHERE owner_kind='entity'"
                            ).fetchone()[0] == 0
        vector_table = "chunk_vectors" if app.extensions["hub"].store.backend == "numpy" else "vec_chunks"
        vector_key = "chunk_id" if vector_table == "chunk_vectors" else "rowid"
        assert conn.execute(
            f"SELECT count(*) FROM {vector_table} WHERE {vector_key}=?", (chunk_id,)
        ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'Important'"
                            ).fetchone()[0] == 1
        audit = conn.execute(
            "SELECT action,detail FROM audit_log WHERE action='type_delete'"
        ).fetchone()
        assert audit["action"] == "type_delete"
        assert "Important detail" not in audit["detail"]
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] >= audit_before + 2
    finally:
        conn.close()
    names = {tool["function"]["name"] for tool in client.get("/api/tools").get_json()}
    assert not any("delete_type" in name for name in names)


def test_policy_flags_affect_tools_audit_capture_and_api(client, app):
    created = _type(client, "private_record", policy={
        "searchable": True, "capture_visible": False, "agent_visible": False, "auditable": False,
    })
    assert created.status_code == 201
    info = client.get("/api/entity-types/private_record").get_json()["type"]
    assert info["embed"] is True
    assert info["policy"] == {
        "searchable": True, "capture_visible": False, "agent_visible": False,
        "auditable": False, "allow_extension": True,
    }
    entity = client.post("/api/entities", json={
        "type": "private_record", "data": {"title": "Hidden phrase"},
    }).get_json()["entity"]
    assert client.patch(f"/api/entities/{entity['id']}", json={"title": "Hidden changed"}
                        ).status_code == 200
    assert client.post(f"/api/entities/{entity['id']}/archive", json={}).status_code == 200
    assert client.post(f"/api/entities/{entity['id']}/restore", json={}).status_code == 200

    names = {item["name"] for item in _tool(client, "list_entity_types", {})["result"]["types"]}
    assert "private_record" not in names
    assert _tool(client, "query_entities", {"type": "private_record"})["error"]["code"] == "not_found"
    assert _tool(client, "get_entity", {"id": entity["id"]})["error"]["code"] == "not_found"
    assert _tool(client, "create_entity", {
        "type": "private_record", "data": {"title": "No write"},
    })["error"]["code"] == "not_found"
    assert _tool(client, "update_entity", {
        "id": entity["id"], "patch": {"title": "No write"},
    })["error"]["code"] == "not_found"
    assert _tool(client, "supersede_entity", {
        "id": entity["id"], "data": {"title": "No write"},
    })["error"]["code"] == "not_found"
    assert _tool(client, "archive_entity", {
        "id": entity["id"],
    })["error"]["code"] == "not_found"
    search = _tool(client, "search_knowledge", {"q": "Hidden phrase"})
    assert all(hit.get("id") != entity["id"] for hit in search["result"]["hits"])

    conn = app.extensions["hub"].conn()
    try:
        assert conn.execute(
            "SELECT count(*) FROM audit_log WHERE target_uid=?", (entity["id"],)
        ).fetchone()[0] == 0
        assert context.capture_excluded_types(conn, "") >= {"private_record"}
        assert client.delete(f"/api/entities/{entity['id']}?confirm=true").status_code == 200
        assert conn.execute(
            "SELECT count(*) FROM audit_log WHERE target_uid=? AND action='forget'",
            (entity["id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()
    assert client.patch("/api/entity-types/private_record", json={
        "policy": {"agent_visible": True, "auditable": True},
    }).status_code == 200
    assert client.patch("/api/entity-types/private_record", json={
        "policy": {"unexpected": False},
    }).status_code == 400


def test_policy_migration_preserves_existing_type(tmp_path):
    path = str(tmp_path / "before-policy.db")
    conn = db.connect(path, load_vec=False)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for version, migrate in enumerate(db.MIGRATIONS[:4], start=1):
            migrate(conn)
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),),
            )
        conn.execute(
            "INSERT INTO entity_types(name,description,json_schema) VALUES(?,?,?)",
            ("legacy_type", "Existing", json.dumps({
                "type": "object", "properties": {"title": {"type": "string"}},
            })),
        )
        db.migrate(conn)
        row = conn.execute(
            "SELECT capture_visible,agent_visible,auditable,allow_extension "
            "FROM entity_types WHERE name=?",
            ("legacy_type",),
        ).fetchone()
        assert tuple(row) == (1, 1, 1, 1)
        assert db.get_meta(conn, "schema_version") == "8"
    finally:
        conn.close()
