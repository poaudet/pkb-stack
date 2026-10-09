import json

import pytest

from hub import create_app
from tests.test_capture import Stub, make_capture_client, submit, wait_capture


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.close()


def test_capture_list_filters_and_summary(stub, tmp_path):
    app, client = make_capture_client(tmp_path, stub)
    first = submit(client, "Working with Matt on PMT since June, launch is Oct 13.")
    ready = wait_capture(client, first)

    response = client.get("/api/capture?state=ready&source=ui")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    item = payload["captures"][0]
    assert item == {
        "id": first,
        "state": "ready",
        "source": "ui",
        "created_at": ready["created_at"],
        "summary": ready["plan"]["summary"],
        "model": "stub",
        "op_count": 3,
    }
    assert payload["items"][0]["id"] == first


def test_validate_reports_dependencies_without_writing(stub, tmp_path):
    app, client = make_capture_client(tmp_path, stub)
    capture_id = submit(client)
    capture = wait_capture(client, capture_id)
    ops = capture["plan"]["ops"]
    conn = app.extensions["hub"].conn()
    try:
        before = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        capture_ops_before = conn.execute("SELECT count(*) FROM capture_ops").fetchone()[0]
    finally:
        conn.close()

    response = client.post(
        f"/api/capture/{capture_id}/validate",
        json={"ops": [{"id": ops[2]["id"], "include": True},
                      {"id": ops[0]["id"], "include": False}]},
    )

    assert response.status_code == 200
    validation = response.get_json()
    assert validation["selection"]["ok"] is False
    assert validation["selection"]["missing"]
    assert {op["id"] for op in validation["ops"]} == {op["id"] for op in ops}
    assert client.get(f"/api/capture/{capture_id}").get_json()["capture"]["state"] == "ready"
    conn = app.extensions["hub"].conn()
    try:
        assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM capture_ops").fetchone()[0] == capture_ops_before
    finally:
        conn.close()


def test_apply_accepts_revalidated_add_properties_override(stub, tmp_path):
    app, client = make_capture_client(tmp_path, stub)
    owner = client.put("/api/self", json={"name": "Taylor"}).get_json()["self"]
    stub.reply = json.dumps({
        "summary": "Favorite color",
        "ops": [
            {"id": "field", "op": "extend_entity_type", "type": "person",
             "add_properties": {"favorite_color": {
                 "type": "string", "description": "Favorite color",
             }}},
            {"id": "owner", "op": "update_entity", "target": "SELF",
             "patch": {"favorite_color": "blue"}},
        ],
        "keep_note": False,
        "questions": [],
    })
    capture_id = submit(client, "My favorite color is blue.")
    capture = wait_capture(client, capture_id)
    extension = next(op for op in capture["plan"]["ops"] if op["kind"] == "extend_entity_type")
    update = next(op for op in capture["plan"]["ops"] if op["kind"] == "update_entity")
    override = {"type": "string", "description": "Preferred color"}

    validated = client.post(f"/api/capture/{capture_id}/validate", json={
        "ops": [{"id": extension["id"], "add_properties": {
            "favorite_color": override,
        }}],
    })

    assert validated.status_code == 200
    assert validated.get_json()["selection"]["ok"] is True
    history = client.get("/api/entity-types/person/history").get_json()["history"]
    assert history[-1]["version"] == 1

    applied = client.post(f"/api/capture/{capture_id}/apply", json={
        "ops": [{"id": extension["id"], "add_properties": {
            "favorite_color": override,
        }}],
    })

    assert applied.status_code == 200, applied.get_json()
    person = client.get(f"/api/entities/{owner['id']}").get_json()["entity"]
    assert person["data"]["favorite_color"] == "blue"
    assert client.get("/api/entity-types/person").get_json()["type"]["json_schema"][
        "properties"
    ]["favorite_color"]["description"] == "Preferred color"


def test_entity_labels_include_archived_and_omit_unknown(client):
    entity = client.post("/api/entities", json={
        "type": "person", "data": {"name": "Sam", "relationship": "friend"},
    }).get_json()["entity"]
    assert client.post(f"/api/entities/{entity['id']}/archive", json={}).status_code == 200

    response = client.post("/api/entities/labels", json={
        "ids": [entity["id"], "does-not-exist"],
    })

    assert response.status_code == 200
    assert response.get_json() == {
        "labels": {entity["id"]: {"type": "person", "label": "Sam"}},
    }
    assert client.post("/api/entities/labels", json={"ids": ["id"] * 101}).status_code == 400
