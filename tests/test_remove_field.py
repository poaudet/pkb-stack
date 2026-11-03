from __future__ import annotations

import pytest

from tests.test_hub_improvements import _tool, _type


@pytest.fixture
def client(mk):
    return mk().test_client()


def _book(client):
    r = client.post("/api/entity-types", json={
        "name": "gadget", "unique_key": "title", "text_template": "{title} {color}",
        "json_schema": {"type": "object", "required": ["title"], "properties": {
            "title": {"type": "string"}, "color": {"type": "string"}}},
    })
    assert r.status_code == 201, r.get_json()
    ids = []
    for i in range(7):
        r = client.post("/api/entities", json={"type": "gadget",
                                               "data": {"title": f"g{i}", "color": f"red{i}"}})
        assert r.status_code == 201, r.get_json()
        ids.append(r.get_json()["entity"]["id"])
    return ids


def test_dry_run_then_confirm_strips_values_and_logs(client, app):
    _book(client)
    url = "/api/entity-types/gadget/remove-field"
    dry = client.post(url, json={"field": "color"}).get_json()
    assert dry["applied"] is False and dry["count"] == 7 and len(dry["sample_ids"]) == 5
    assert "color" in client.get("/api/entity-types/gadget").get_json()["type"]["json_schema"]["properties"]
    done = client.post(url, json={"field": "color", "confirm": True}).get_json()
    assert done["applied"] and done["schema_version"] == 2
    t = client.get("/api/entity-types/gadget").get_json()["type"]
    assert "color" not in t["json_schema"]["properties"]
    listed = client.get("/api/entities?type=gadget&limit=50").get_json()["items"]
    assert all("color" not in e["data"] for e in listed)
    conn = app.extensions["hub"].conn()
    try:
        rows = conn.execute("SELECT removed_value FROM schema_change_log").fetchall()
        assert len(rows) == 7
        assert conn.execute("SELECT count(*) FROM chunks WHERE text LIKE '%red%'").fetchone()[0] == 0
    finally:
        conn.close()


def test_refusals(client):
    _book(client)
    url = "/api/entity-types/gadget/remove-field"
    assert client.post(url, json={"field": "title", "confirm": True}).status_code == 409
    assert client.post(url, json={"field": "nope"}).status_code == 404
    assert _type(client, "locked", policy={"allow_extension": False}).status_code == 201
    assert client.post("/api/entity-types/locked/remove-field",
                       json={"field": "x"}).status_code == 403


def test_tool_is_destructive_gated_and_dry_runs_by_default(client, mk):
    assert _tool(client, "remove_entity_type_field", {"name": "gadget", "field": "color"})[
        "error"]["code"] == "not_found"
    enabled = mk(tools_allow_destructive=True).test_client()
    _book(enabled)
    out = _tool(enabled, "remove_entity_type_field", {"name": "gadget", "field": "color"})
    assert out["ok"] and out["result"]["applied"] is False and out["result"]["count"] == 7
    out = _tool(enabled, "remove_entity_type_field",
                {"name": "gadget", "field": "color", "confirm": True})
    assert out["result"]["applied"] is True
