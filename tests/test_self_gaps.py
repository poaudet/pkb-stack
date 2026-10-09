import json

import pytest

from hub import create_app, entities
from hub.capture import context
from tests.conftest import make_settings
from tests.test_capture import Stub, make_capture_client, submit, wait_capture


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.close()


def _reply(stub, ops):
    stub.reply = json.dumps({
        "summary": "Capture", "ops": ops, "keep_note": False, "questions": [],
    })


def test_self_api_guards_and_owner_name(tmp_path):
    app = create_app(make_settings(tmp_path, owner_name="Alex"))
    client = app.test_client()
    configured = client.get("/api/self").get_json()
    assert configured["self"]["data"]["name"] == "Alex"
    assert configured["configured"] is True
    owner_id = configured["self"]["id"]

    second = client.post("/api/entities", json={
        "type": "person", "data": {"name": "Other", "relationship": "self"},
    })
    assert second.status_code == 409
    assert second.get_json()["error"]["code"] == "conflict"
    assert client.patch(f"/api/entities/{owner_id}", json={
        "relationship": "friend",
    }).get_json()["error"]["code"] == "protected"
    assert client.post(f"/api/entities/{owner_id}/archive", json={}).get_json()[
        "error"
    ]["code"] == "protected"

    changed = client.patch(f"/api/entities/{owner_id}?force=true", json={
        "relationship": "friend",
    })
    assert changed.status_code == 200
    assert client.get("/api/self").get_json()["self"] is None


def test_self_repairs_pointer_and_reports_duplicate_conflicts(tmp_path):
    app = create_app(make_settings(tmp_path))
    client = app.test_client()
    first = client.put("/api/self", json={"name": "Sam"}).get_json()["self"]
    conn = app.extensions["hub"].conn()
    try:
        conn.execute(
            "INSERT INTO entities(uid,type,data) VALUES(?,?,?)",
            ("duplicate-owner", "person", json.dumps({
                "name": "Sam Two", "relationship": "Self",
            })),
        )
        conn.execute("UPDATE meta SET value='stale' WHERE key='self_uid'")
    finally:
        conn.close()
    result = client.get("/api/self").get_json()
    assert result["self"]["id"] == first["id"]
    assert result["conflicts"] == ["duplicate-owner"]


def test_capture_self_resolution_and_setup_gap(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    owner = client.put("/api/self", json={"name": "Taylor"}).get_json()["self"]
    _reply(stub, [{
        "id": "self-update", "op": "update_entity", "target": "SELF",
        "patch": {"interests": ["kiwis"]},
    }])
    uid = submit(client, "I like kiwis a lot.")
    capture = wait_capture(client, uid)
    op = capture["plan"]["ops"][0]
    assert op["target"] == owner["id"]
    assert "SELF |" in stub.calls[0]["messages"][0]["content"]
    assert "Recurring routines or activities" in stub.calls[0]["messages"][0]["content"]
    applied = client.post(f"/api/capture/{uid}/apply", json={})
    assert applied.status_code == 200
    assert client.get("/api/self").get_json()["self"]["data"]["interests"] == ["kiwis"]

    _app2, client2 = make_capture_client(tmp_path / "no-owner", stub)
    uid2 = submit(client2, "I like kiwis a lot.")
    capture2 = wait_capture(client2, uid2)
    op2 = capture2["plan"]["ops"][0]
    assert op2["status"] == "invalid"
    assert op2["error_code"] == "self_not_set"
    assert capture2["plan"]["gaps"] == [{"kind": "self", "action": "set_owner"}]
    assert len(capture2["plan"]["questions"]) == 1
    client2.put("/api/self", json={"name": "Taylor"})
    regenerated = client2.post(f"/api/capture/{uid2}/regenerate", json={})
    assert regenerated.status_code == 202
    assert wait_capture(client2, uid2)["plan"]["ops"][0]["status"] in ("valid", "converted")


def test_capture_database_refs_gap_fill_and_dependencies(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    _reply(stub, [{
        "id": "event", "op": "create_entity", "type": "event",
        "data": {"date": "2026-10-09", "title": "Portland trip", "people": ["Sara"]},
    }])
    uid = submit(client, "Went to Portland with Sara last Saturday.")
    capture = wait_capture(client, uid)
    ops = capture["plan"]["ops"]
    gap = next(op for op in ops if op.get("gap", {}).get("kind") == "ref")
    event = next(op for op in ops if op["kind"] == "create_entity" and op["type"] == "event")
    assert gap["include"] is True and gap["risk"] == "auto_gap"
    assert event["data"]["people"] == [{"$ref": gap["ref"]}]
    assert gap["id"] in event["requires"]

    refused = client.post(f"/api/capture/{uid}/apply", json={
        "ops": [{"id": gap["id"], "include": False}],
    })
    assert refused.status_code == 409
    assert refused.get_json()["error"]["code"] == "dependency"
    assert refused.get_json()["error"]["missing"] == [
        {"op": event["id"], "requires": [gap["id"]]}
    ]

    applied = client.post(f"/api/capture/{uid}/apply", json={})
    assert applied.status_code == 200, applied.get_json()
    event_id = next(item["id"] for item in applied.get_json()["applied"]
                    if item["type"] == "event")
    person = client.get("/api/entities").get_json()["items"]
    sara = next(item for item in person if item["data"].get("name") == "Sara")
    assert client.get(f"/api/entities/{event_id}").get_json()["entity"]["data"][
        "people"
    ] == [sara["id"]]


def test_capture_reference_resolution_and_type_similarity(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    sara = client.post("/api/entities", json={
        "type": "person", "data": {"name": "Sara"},
    }).get_json()["entity"]
    _reply(stub, [{
        "id": "event", "op": "create_entity", "type": "event",
        "data": {"date": "2026-10-09", "title": "Coffee", "people": ["Sara"]},
    }])
    capture = wait_capture(client, submit(client, "Coffee with Sara."))
    assert capture["plan"]["ops"][0]["data"]["people"] == [sara["id"]]
    assert not any(op.get("gap") for op in capture["plan"]["ops"])

    client.post("/api/entity-types", json={
        "name": "animal", "json_schema": {
            "type": "object", "properties": {
                "species": {"type": "string"}, "legs": {"type": "integer"},
            },
        },
    })
    _reply(stub, [
        {"id": "plural", "op": "create_entity_type", "name": "animals",
         "json_schema": {"type": "object", "properties": {"name": {"type": "string"}}}},
        {"id": "similar", "op": "create_entity_type", "name": "pet",
         "json_schema": {"type": "object", "properties": {
             "species": {"type": "string"}, "legs": {"type": "integer"},
         }}},
    ])
    capture2 = wait_capture(client, submit(client, "I saw a pet."))
    pet_type = next(op for op in capture2["plan"]["ops"]
                    if op["kind"] == "create_entity_type")
    assert pet_type["risk"] == "similar_type" and pet_type["include"] is False
    assert any("type exists: animal" in warning for warning in capture2["plan"]["warnings"])


def test_schema_extension_apply_search_and_undo(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub)
    owner = client.put("/api/self", json={"name": "Taylor"}).get_json()["self"]
    _reply(stub, [
        {"id": "extend", "op": "extend_entity_type", "type": "person",
         "add_properties": {"favorite_color": {
             "type": "string", "description": "Favorite color",
         }}},
        {"id": "update", "op": "update_entity", "target": "SELF",
         "patch": {"favorite_color": "blue"}},
    ])
    uid = submit(client, "My favorite color is blue.")
    capture = wait_capture(client, uid)
    extend = next(op for op in capture["plan"]["ops"] if op["kind"] == "extend_entity_type")
    update = next(op for op in capture["plan"]["ops"] if op["kind"] == "update_entity")
    assert extend["include"] is False and update["requires"] == [extend["id"]]

    refused = client.post(f"/api/capture/{uid}/apply", json={
        "ops": [{"id": update["id"], "include": True}],
    })
    assert refused.status_code == 409
    assert refused.get_json()["error"]["code"] == "dependency"
    applied = client.post(f"/api/capture/{uid}/apply", json={
        "ops": [{"id": extend["id"], "include": True},
                {"id": update["id"], "include": True}],
    })
    assert applied.status_code == 200, applied.get_json()
    schema = client.get("/api/entity-types/person").get_json()["type"]
    assert schema["schema_version"] == 2
    assert schema["json_schema"]["properties"]["favorite_color"]["x-origin"] == "capture"
    history = client.get("/api/entity-types/person/history").get_json()["history"]
    assert history[-1]["change"] == {"added": ["favorite_color"]}
    assert client.get(f"/api/entities/{owner['id']}").get_json()["entity"]["data"][
        "favorite_color"
    ] == "blue"
    search = client.post("/api/search", json={"q": "blue", "kinds": ["entity"]})
    assert owner["id"] in [hit["id"] for hit in search.get_json()["hits"]]

    undone = client.post(f"/api/capture/{uid}/undo", json={})
    assert undone.status_code == 200, undone.get_json()
    assert undone.get_json()["kept_in_use"] == []
    restored = client.get(f"/api/entities/{owner['id']}").get_json()["entity"]
    assert "favorite_color" not in restored["data"]
    schema = client.get("/api/entity-types/person").get_json()["type"]
    assert schema["schema_version"] == 3
    assert "favorite_color" not in schema["json_schema"]["properties"]
    assert context.is_first_person("J'aime la randonnée et my notes")


def test_extension_validation_existing_data_and_capture_field_cap(tmp_path, stub):
    _app, client = make_capture_client(tmp_path, stub, capture_max_new_fields=5)
    owner = client.put("/api/self", json={"name": "Taylor"}).get_json()["self"]
    rejected = (
        ("uid", {"type": "string", "description": "Identifier"}),
        ("name", {"type": "string", "description": "Duplicate"}),
        ("api_token", {"type": "string", "description": "Credential"}),
        ("bad_enum", {"type": "string", "description": "Enum field", "enum": ["x"]}),
        ("bad_object", {"type": "object", "description": "Object field"}),
        ("no_description", {"type": "string"}),
    )
    for name, definition in rejected:
        response = client.post("/api/entity-types/person/extend", json={
            "add_properties": {name: definition}, "confirm": True,
        })
        assert response.status_code == 400, response.get_json()

    conn = _app.extensions["hub"].conn()
    try:
        conn.execute(
            "UPDATE entities SET data=? WHERE uid=?",
            (json.dumps({"name": "Taylor", "favorite_color": 7}), owner["id"]),
        )
    finally:
        conn.close()
    conflict = client.post("/api/entity-types/person/extend", json={
        "add_properties": {"favorite_color": {
            "type": "string", "description": "Favorite color",
        }},
        "confirm": True,
    })
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "schema_conflict"

    _clean_app, clean_client = make_capture_client(tmp_path / "clean", stub)
    clean_owner = clean_client.put("/api/self", json={"name": "Taylor"}).get_json()["self"]
    fields = {
        f"field_{index}": {"type": "string", "description": f"Field {index} value"}
        for index in range(6)
    }
    _reply(stub, [
        {"id": "extend", "op": "extend_entity_type", "type": "person",
         "add_properties": fields},
        {"id": "update", "op": "update_entity", "target": "SELF",
         "patch": {"field_0": "value"}},
    ])
    capture = wait_capture(clean_client, submit(clean_client, "My field is value."))
    extension = next(op for op in capture["plan"]["ops"]
                     if op["kind"] == "extend_entity_type")
    assert len(extension["add_properties"]) == 3
    assert any("truncated 3 new fields" in warning for warning in extension["warnings"])
    assert clean_owner["id"] == clean_client.get("/api/self").get_json()["self"]["id"]


def test_render_entity_extras_order_filter_and_cap():
    schema = {"properties": {
        "name": {"type": "string"},
        "color": {"type": "string"},
        "details": {"type": "object"},
        "friend": {"type": "string", "x-ref": "person"},
        "habits": {"type": "array", "items": {"type": "string"}},
    }}
    text = entities.render_text("person", "{name}", {
        "name": "Taylor", "color": "blue", "details": {"secret": "hidden"},
        "friend": "uid", "habits": ["running", "hiking"],
    }, schema)
    assert text == "Taylor; color: blue; habits: running, hiking"
    large_schema = {"properties": {
        "name": {"type": "string"},
        "first": {"type": "string"},
        "second": {"type": "string"},
    }}
    capped = entities.render_text(
        "person", "{name}", {"name": "Taylor", "first": "a" * 700, "second": "b" * 700},
        large_schema,
    )
    assert len(capped) - len("Taylor") <= 800
