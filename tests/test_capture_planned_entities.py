import json

from hub import entities
from hub.capture import merge
from hub.errors import ApiError
from tests.test_capture import make_capture_client, stub, submit, wait_capture  # noqa: F401


def event(**extra):
    return {"id": "o2", "op": "create_entity", "type": "event",
            "data": {"title": "Synthetic lunch", "date": "2026-11-05", **extra}}


def person(op_id="o1", ref="alex", **data):
    return {"id": op_id, "op": "create_entity", "type": "person", "ref": ref,
            "data": {"name": "Alex Example", **data}}


def ready(tmp_path, stub, ops):  # noqa: F811
    app, client = make_capture_client(tmp_path, stub)
    stub.reply = json.dumps({"ops": ops, "keep_note": False, "note": None, "questions": []})
    uid = submit(client, "synthetic lunch with Alex Example")
    capture = wait_capture(client, uid)
    assert capture["state"] == "ready"
    return app, client, uid, capture["plan"]["ops"]


def rows(app, kind):
    conn = app.extensions["hub"].conn()
    try:
        return [(row["uid"], json.loads(row["data"])) for row in conn.execute(
            "SELECT uid,data FROM entities WHERE type=? AND status='current'", (kind,))]
    finally:
        conn.close()


def count(app):
    conn = app.extensions["hub"].conn()
    try:
        return conn.execute("SELECT count(*) FROM entities").fetchone()[0]
    finally:
        conn.close()


def by_id(ops):
    return {op["id"]: op for op in ops}


def test_planned_refs_are_stable_and_usable_in_later_ops(tmp_path, stub):  # noqa: F811
    app, client, uid, ops = ready(tmp_path, stub, [
        person(), event(people=["@new:person:1"])])
    ops = by_id(ops)
    assert ops["o1"]["planned_ref"] == "@new:person:1"
    assert ops["o2"]["planned_ref"] == "@new:event:1"
    assert ops["o2"]["data"]["people"] == [{"$ref": "@new:person:1"}]
    assert ops["o2"]["requires"] == ["o1"] and ops["o2"]["status"] == "valid"
    r = client.post(f"/api/capture/{uid}/apply", json={}); assert r.status_code == 200, r.get_json()
    (person_uid, _), = rows(app, "person")
    (_, event_data), = rows(app, "event")
    assert event_data["people"] == [person_uid]


def test_forward_reference_is_ordered_before_apply(tmp_path, stub):  # noqa: F811
    app, client, uid, ops = ready(tmp_path, stub, [
        event(people=[{"$ref": "alex"}]), person(op_id="o1")])
    assert [op["id"] for op in ops] == ["o1", "o2"]
    assert all(op["status"] == "valid" for op in ops)
    r = client.post(f"/api/capture/{uid}/apply", json={}); assert r.status_code == 200, r.get_json()
    (person_uid, _), = rows(app, "person")
    assert rows(app, "event")[0][1]["people"] == [person_uid]


def test_edit_planned_entity_persists_without_writing_rows(tmp_path, stub):  # noqa: F811
    app, client, uid, _ops = ready(tmp_path, stub, [person(), event(people=[{"$ref": "alex"}])])
    before = count(app)
    edit = {"ops": [{"id": "o1", "include": True,
                     "data": {"name": "Alex Example", "relationship": "friend"}}],
            "persist": True}
    response = client.post(f"/api/capture/{uid}/validate", json=edit)
    assert response.status_code == 200 and response.get_json()["selection"]["ok"]
    stored = by_id(client.get(f"/api/capture/{uid}").get_json()["capture"]["plan"]["ops"])
    assert stored["o1"]["data"]["relationship"] == "friend"
    assert stored["o1"]["planned_ref"] == "@new:person:1"
    assert count(app) == before
    client.post(f"/api/capture/{uid}/validate", json=edit)
    again = by_id(client.get(f"/api/capture/{uid}").get_json()["capture"]["plan"]["ops"])
    assert again["o1"]["planned_ref"] == "@new:person:1"
    r = client.post(f"/api/capture/{uid}/apply", json={}); assert r.status_code == 200, r.get_json()
    assert rows(app, "person")[0][1]["relationship"] == "friend"


def test_unpersisted_validation_leaves_plan_alone_and_unticked_stays_unticked(
        tmp_path, stub):  # noqa: F811
    _app, client, uid, _ops = ready(tmp_path, stub, [person(), event(people=[{"$ref": "alex"}])])
    client.post(f"/api/capture/{uid}/validate", json={"ops": [
        {"id": "o1", "data": {"name": "Changed Name"}}]})
    stored = by_id(client.get(f"/api/capture/{uid}").get_json()["capture"]["plan"]["ops"])
    assert stored["o1"]["data"]["name"] == "Alex Example"
    client.post(f"/api/capture/{uid}/validate", json={"persist": True, "ops": [
        {"id": "o1", "include": False}, {"id": "o2", "include": False}]})
    stored = by_id(client.get(f"/api/capture/{uid}").get_json()["capture"]["plan"]["ops"])
    assert stored["o1"]["include"] is False and stored["o2"]["include"] is False


def test_excluding_a_needed_create_blocks_dependents_with_reason(tmp_path, stub):  # noqa: F811
    _app, client, uid, _ops = ready(tmp_path, stub, [person(), event(people=[{"$ref": "alex"}])])
    body = {"ops": [{"id": "o1", "include": False}]}
    result = client.post(f"/api/capture/{uid}/validate", json=body).get_json()
    blocked = by_id(result["ops"])["o2"]["blocked_by"]
    assert blocked[0]["op"] == "o1" and blocked[0]["planned_ref"] == "@new:person:1"
    assert "needs" in blocked[0]["reason"] and result["selection"]["ok"] is False
    refused = client.post(f"/api/capture/{uid}/apply", json=body)
    assert refused.status_code == 409
    assert refused.get_json()["error"]["code"] == "dependency"
    assert "o2" in refused.get_json()["error"]["message"]


def test_editing_a_reference_to_a_new_name_creates_a_distinct_gap(tmp_path, stub):  # noqa: F811
    app, client, uid, ops = ready(tmp_path, stub, [event(people=["Alex Example"])])
    assert [op["id"] for op in ops if op.get("gap")] == ["g1"]
    edit = {"persist": True, "ops": [{"id": "o2", "data": {
        "title": "Synthetic lunch", "date": "2026-11-05",
        "people": [{"$ref": "g1"}, "Blake Example"]}}]}
    result = client.post(f"/api/capture/{uid}/validate", json=edit).get_json()
    ids = [op["id"] for op in result["ops"]]
    assert len(ids) == len(set(ids)) == 3
    stored = client.get(f"/api/capture/{uid}").get_json()["capture"]["plan"]["ops"]
    assert sorted(op["planned_ref"] for op in stored if op["type"] == "person") == [
        "@new:person:1", "@new:person:2"]
    r = client.post(f"/api/capture/{uid}/apply", json={}); assert r.status_code == 200, r.get_json()
    assert sorted(data["name"] for _uid, data in rows(app, "person")) == [
        "Alex Example", "Blake Example"]
    assert len(rows(app, "event")[0][1]["people"]) == 2


def test_duplicate_planned_creates_merge_and_repoint(tmp_path, stub):  # noqa: F811
    app, client, uid, ops = ready(tmp_path, stub, [
        person(op_id="o1", ref="a"),
        person(op_id="o3", ref="b", relationship="friend"),
        event(people=[{"$ref": "a"}, {"$ref": "b"}])])
    persons = [op for op in ops if op["type"] == "person"]
    assert len(persons) == 1 and persons[0]["data"]["relationship"] == "friend"
    assert any("merged duplicate" in warning for warning in persons[0]["warnings"])
    r = client.post(f"/api/capture/{uid}/apply", json={}); assert r.status_code == 200, r.get_json()
    (person_uid, _), = rows(app, "person")
    assert rows(app, "event")[0][1]["people"] == [person_uid]


def test_apply_failure_rolls_back_every_op_and_names_the_op(tmp_path, stub, monkeypatch):  # noqa: F811
    app, client, uid, _ops = ready(tmp_path, stub, [person(), event(people=[{"$ref": "alex"}])])
    original = entities.create_entity
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ApiError("conflict", "synthetic failure", 409)
        return original(*args, **kwargs)

    monkeypatch.setattr(entities, "create_entity", flaky)
    before = count(app)
    response = client.post(f"/api/capture/{uid}/apply", json={})
    assert response.status_code == 409
    assert response.get_json()["error"]["op_id"] == "o2"
    assert count(app) == before


def test_reference_across_chunks_merges_to_one_create():
    chunk_one = {"ops": [{"id": "c1_o1", "op": "create_entity", "type": "person",
                          "ref": "c1_alex", "data": {"name": "Alex Example"}}]}
    chunk_two = {"ops": [
        {"id": "c2_o1", "op": "create_entity", "type": "person", "ref": "c2_alex",
         "data": {"name": "Alex Example"}},
        {"id": "c2_o2", "op": "create_entity", "type": "event",
         "data": {"title": "Synthetic lunch", "date": "2026-11-05",
                  "people": [{"$ref": "c2_alex"}]}}]}
    ops, _provenance, _warnings, _plan_warnings = merge.merge(
        [(1, chunk_one), (2, chunk_two)], 20)
    assert [op["id"] for op in ops] == ["c1_o1", "c2_o2"]
    assert ops[1]["data"]["people"] == [{"$ref": "c1_alex"}]
