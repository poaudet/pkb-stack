import importlib.util
import json
from pathlib import Path

import pytest


def mkent(client, t, data, **kw):
    return client.post("/api/entities", json={"type": t, "data": data}, **kw)


def q(client, body):
    r = client.post("/api/entities/query", json=body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["items"]


def test_seed_types_and_validation(client):
    names = {t["name"] for t in client.get("/api/entity-types").get_json()["types"]}
    assert {"person", "event", "message", "memory"} <= names
    assert mkent(client, "person", {}).status_code == 400  # name required
    assert mkent(client, "person", {"name": "A", "birthday": "1990-13-40"}).status_code == 400
    assert mkent(client, "person", {"name": "A", "birthday": "1990-02-03"}).status_code == 201
    assert mkent(client, "message", {"contact": "x", "at": "nope", "text": "t"}).status_code == 400
    assert mkent(client, "nope", {"a": 1}).status_code == 404
    assert client.delete("/api/entity-types/person").status_code == 400
    assert client.delete("/api/entity-types/person?confirm=true").status_code == 409


def test_unique_key_and_supersede(client):
    a = mkent(client, "person", {"name": "Alice"}).get_json()["entity"]
    r = mkent(client, "person", {"name": "ALICE"})
    assert r.status_code == 409 and r.get_json()["error"]["existing"] == a["id"]
    r = client.post(f"/api/entities/{a['id']}/supersede",
                    json={"data": {"name": "Alice", "relationship": "friend"}, "reason": "upd"})
    assert r.status_code == 201
    new = r.get_json()["entity"]
    old = client.get(f"/api/entities/{a['id']}").get_json()["entity"]
    assert old["status"] == "superseded" and old["superseded_by"] == new["id"]
    assert len(q(client, {"type": "person"})) == 1
    assert len(q(client, {"type": "person", "include_superseded": True})) == 2
    hits = client.post("/api/search", json={"q": "Alice", "kinds": ["entity"]}).get_json()["hits"]
    assert [h["id"] for h in hits] == [new["id"]] and hits[0]["type"] == "person"
    assert client.post("/api/search", json={"q": "Alice", "kinds": ["note"]}).get_json()["hits"] == []


def test_merge_patch_archive_forget(client, app):
    e = mkent(client, "person", {"name": "Bob", "habits": ["a"], "notes": "x"}).get_json()["entity"]
    r = client.patch(f"/api/entities/{e['id']}", json={"notes": None, "habits": ["b", "c"],
                                                       "contact": {"email": "b@x.y"}})
    d = r.get_json()["entity"]["data"]
    assert d == {"name": "Bob", "habits": ["b", "c"], "contact": {"email": "b@x.y"}}
    assert client.patch(f"/api/entities/{e['id']}", json={"name": 5}).status_code == 400
    assert client.post(f"/api/entities/{e['id']}/archive", json={}).status_code == 200
    assert q(client, {"type": "person"}) == []
    assert client.post(f"/api/entities/{e['id']}/restore").status_code == 200
    assert client.delete(f"/api/entities/{e['id']}").status_code == 400
    assert client.delete(f"/api/entities/{e['id']}?confirm=true").status_code == 200
    conn = app.extensions["hub"].conn()
    assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunks WHERE owner_kind='entity'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'Bob'"
                        ).fetchone()[0] == 0
    row = conn.execute("SELECT detail FROM audit_log WHERE action='forget'").fetchone()
    assert row["detail"] is None
    conn.close()


def test_query_ops(client):
    for n, rel, b, hab in (("Ann", "friend", "1990-01-05", ["run"]),
                           ("Ben", "Friend", "1985-06-10", ["chess"]),
                           ("Cy", "family", None, [])):
        d = {"name": n, "relationship": rel, "habits": hab}
        if b:
            d["birthday"] = b
        assert mkent(client, "person", d).status_code == 201

    def names(**kw):
        return [e["data"]["name"] for e in q(client, {"type": "person", **kw})]

    def f(op, value=None, field="relationship"):
        return {"filters": [{"field": field, "op": op, "value": value}], "order_by":
                {"field": "name", "dir": "asc"}}

    assert names(**f("eq", "friend")) == ["Ann"]
    assert names(**f("ieq", "FRIEND")) == ["Ann", "Ben"]
    assert names(**f("ne", "friend")) == ["Ben", "Cy"]
    assert names(**f("contains", "rie")) == ["Ann", "Ben"]
    assert names(**f("contains", "%")) == []
    assert names(**f("in", ["family", "friend"])) == ["Ann", "Cy"]
    assert names(**f("gt", "1986-01-01", "birthday")) == ["Ann"]
    assert names(**f("gte", "1985-06-10", "birthday")) == ["Ann", "Ben"]
    assert names(**f("lt", "1986-01-01", "birthday")) == ["Ben"]
    assert names(**f("lte", "1985-06-10", "birthday")) == ["Ben"]
    assert names(**f("exists", True, "birthday")) == ["Ann", "Ben"]
    assert names(**f("exists", False, "birthday")) == ["Cy"]
    assert names(**f("has", "CHESS", "habits")) == ["Ben"]
    # NULLs last in both directions, ordering works
    assert names(order_by={"field": "birthday", "dir": "asc"}) == ["Ben", "Ann", "Cy"]
    assert names(order_by={"field": "birthday", "dir": "desc"}) == ["Ann", "Ben", "Cy"]
    assert names(limit=1, offset=1, order_by={"field": "name"}) == ["Ben"]


@pytest.mark.parametrize("field", ["name') OR 1=1 --", "nope", "name; DROP TABLE entities", 5, ""])
def test_query_injection_rejected(client, field):
    mkent(client, "person", {"name": "Z"})
    body = {"type": "person", "filters": [{"field": field, "op": "eq", "value": "x"}]}
    assert client.post("/api/entities/query", json=body).status_code == 400
    body = {"type": "person", "order_by": {"field": field, "dir": "asc"}}
    assert client.post("/api/entities/query", json=body).status_code == 400
    body = {"type": "person", "order_by": {"field": "name", "dir": "asc; DROP"}}
    assert client.post("/api/entities/query", json=body).status_code == 400
    bad = {"type": "person", "filters": [{"field": "name", "op": "like", "value": "x"}]}
    assert client.post("/api/entities/query", json=bad).status_code == 400
    assert client.get("/api/entities?type=person").get_json()["total"] == 1


def test_custom_type_rules(client):
    def mk(schema, **kw):
        return client.post("/api/entity-types", json={"name": "car", "json_schema": schema, **kw})

    ok = {"type": "object", "properties": {"plate": {"type": "string"},
                                           "km": {"type": "integer"}}}
    assert mk({"type": "array"}).status_code == 400
    assert mk({"type": "object", "properties": {}}).status_code == 400
    assert mk({"type": "object", "properties": {"a": {"type": "array",
                                                     "items": {"type": "object"}}}}
              ).status_code == 400
    assert mk({"type": "object", "properties": {"a": {"$ref": "http://x/y"}}}).status_code == 400
    assert mk({"type": "object", "properties": {"a": {"type": "wat"}}}).status_code == 400
    assert mk(ok, unique_key="nope").status_code == 400
    assert client.post("/api/entity-types", json={"name": "Bad Name", "json_schema": ok}
                       ).status_code == 400
    assert mk(ok, unique_key="plate", text_template="{plate} {km}").status_code == 201
    assert mk(ok).status_code == 409
    assert mkent(client, "car", {"plate": "ABC", "km": 12}).status_code == 201
    assert mkent(client, "car", {"plate": "abc"}).status_code == 409
    assert mkent(client, "car", {"plate": "X", "km": "many"}).status_code == 400
    # additive change ok, removal needs force
    more = {"type": "object", "properties": {**ok["properties"], "color": {"type": "string"}}}
    assert client.patch("/api/entity-types/car", json={"json_schema": more}).status_code == 200
    less = {"type": "object", "properties": {"plate": {"type": "string"}}}
    assert client.patch("/api/entity-types/car", json={"json_schema": less}).status_code == 409
    assert client.patch("/api/entity-types/car?force=true", json={"json_schema": less}
                        ).status_code == 200
    assert client.delete("/api/entity-types/car").status_code == 400
    assert client.delete("/api/entity-types/car?confirm=true").status_code == 409


def test_template_never_formats(client):
    t = {"type": "object", "properties": {"a": {"type": "string"}}}
    client.post("/api/entity-types", json={"name": "tpl", "json_schema": t,
                                           "text_template": "{a} {__class__} {0}"})
    assert mkent(client, "tpl", {"a": "hello"}).status_code == 201


def test_search_finds_entity_by_keyword(client):
    mkent(client, "event", {"date": "2025-06-14", "title": "Hike", "location": "Zanzibar"})
    hits = client.post("/api/search", json={"q": "Zanzibar", "types": ["event"]}).get_json()["hits"]
    assert hits and hits[0]["kind"] == "entity" and "keyword" in hits[0]["matched"]
    assert client.post("/api/search", json={"q": "Zanzibar", "types": ["person"]}
                       ).get_json()["hits"] == []


def _seed(client):
    spec = importlib.util.spec_from_file_location(
        "seed_demo", Path(__file__).parent.parent / "scripts" / "seed_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def call(method, path, body):
        r = client.open(path, method=method, json=body)
        assert r.status_code < 300, r.get_json()
        return r.get_json()

    mod.seed(call)


def tool(client, name, args):
    r = client.post(f"/api/tools/{name}", json=args)
    assert r.status_code == 200
    return r.get_json()


def test_owner_scenarios(client):
    _seed(client)
    r = tool(client, "query_entities", {"type": "event", "filters": [
        {"field": "location", "op": "contains", "value": "mont royal"}],
        "order_by": {"field": "date", "dir": "desc"}, "limit": 1})
    assert r["ok"] and r["result"]["items"][0]["data"]["title"] == "Picnic"
    r = tool(client, "query_entities", {"type": "person", "filters": [
        {"field": "name", "op": "ieq", "value": "alice"}]})
    assert r["result"]["items"][0]["data"]["birthday"] == "1990-04-12"
    r = tool(client, "query_entities", {"type": "message", "filters": [
        {"field": "contact", "op": "ieq", "value": "vet"}],
        "order_by": {"field": "at", "dir": "desc"}, "limit": 20})
    assert [m["data"]["direction"] for m in r["result"]["items"]] == ["in", "out"]
    r = tool(client, "query_entities", {"type": "person", "filters": [
        {"field": "relationship", "op": "ieq", "value": "friend"}]})
    assert sorted(p["data"]["name"] for p in r["result"]["items"]) == ["Alice", "Bob"]


def test_tools_manifest_and_errors(mk):
    c = mk().test_client()
    m = c.get("/api/tools").get_json()
    assert all(t["type"] == "function" and {"name", "description", "parameters"} <=
               set(t["function"]) for t in m)
    names = {t["function"]["name"] for t in m}
    assert "query_entities" in names and "forget_note" not in names
    assert c.post("/api/tools/forget_note", json={"id": "x"}).status_code == 404
    assert c.post("/api/tools/nope", json={}).status_code == 404
    r = tool(c, "query_entities", {"type": "person", "filters": [{"field": "zz", "op": "eq"}]})
    assert r["ok"] is False and r["error"]["code"] == "validation_error"
    r = tool(c, "create_entity", {"type": "person"})
    assert r["ok"] is False
    r = tool(c, "get_entity", {"id": "missing"})
    assert r["ok"] is False and r["error"]["code"] == "not_found"
    d = mk(tools_allow_destructive=True).test_client()
    assert "forget_note" in {t["function"]["name"] for t in d.get("/api/tools").get_json()}


def test_agent_tools_flow(client):
    r = tool(client, "create_note", {"content": "agent says hi"})
    assert r["ok"] and "pending" in r["result"]  # agent -> review queue
    assert client.get("/api/notes").get_json()["total"] == 0
    r = tool(client, "create_entity_type", {"name": "book", "json_schema": {
        "type": "object", "properties": {"title": {"type": "string"}}}})
    assert r["ok"]
    r = tool(client, "create_entity", {"type": "book", "data": {"title": "Dune"}})
    eid = r["result"]["entity"]["id"]
    assert r["result"]["entity"]["source"] == "agent:api"
    assert tool(client, "update_entity", {"id": eid, "patch": {"title": "Dune 2"}})["ok"]
    assert tool(client, "supersede_entity", {"id": eid, "data": {"title": "Dune 3"}})["ok"]
    assert tool(client, "archive_entity", {"id": eid})["ok"]
    acts = client.get("/api/activity?limit=50").get_json()["items"]
    assert {"create", "supersede"} <= {a["action"] for a in acts}
    assert json.dumps(acts).find("Dune") == -1  # no content in the audit feed
    hits = tool(client, "search_knowledge", {"q": "Dune", "types": ["book"]})["result"]["hits"]
    assert len(hits) == 1


def test_stats_include_entities(client):
    mkent(client, "person", {"name": "S"})
    assert client.get("/api/stats").get_json()["entities"]["current"] == 1
