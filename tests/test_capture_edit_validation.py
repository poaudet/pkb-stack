import json

from tests.test_capture import make_capture_client, stub, submit, wait_capture  # noqa: F401

DATELESS = {
    "ops": [
        {"id": "o1", "op": "create_entity", "type": "event",
         "data": {"title": "Synthetic lunch", "kind": "meal"}},
        {"id": "o2", "op": "create_entity", "type": "event",
         "data": {"title": "Synthetic dinner", "date": "2026-11-05"}},
    ],
    "keep_note": False, "note": None, "questions": [],
}


def ready(tmp_path, stub):  # noqa: F811
    app, client = make_capture_client(tmp_path, stub)
    stub.reply = json.dumps(DATELESS)
    uid = submit(client, "synthetic lunch and dinner")
    capture = wait_capture(client, uid)
    assert capture["state"] == "ready"
    return app, client, uid, {op["id"]: op for op in capture["plan"]["ops"]}


def validate(client, uid, ops):
    response = client.post(f"/api/capture/{uid}/validate", json={"ops": ops})
    assert response.status_code == 200, response.get_json()
    return {op["id"]: op for op in response.get_json()["ops"]}, response.get_json()["selection"]


def test_missing_date_reports_field_error_not_a_key_error(tmp_path, stub):  # noqa: F811
    _app, _client, _uid, ops = ready(tmp_path, stub)
    assert ops["o1"]["status"] == "invalid"
    assert any("date" in error for error in ops["o1"]["errors"])
    assert ops["o2"]["status"] == "valid"


def test_revalidation_flips_both_ways_and_isolates_ops(tmp_path, stub):  # noqa: F811
    _app, client, uid, _ops = ready(tmp_path, stub)
    fixed, selection = validate(client, uid, [
        {"id": "o1", "include": True, "data": {"title": "Synthetic lunch", "date": "2026-11-05"}},
    ])
    assert fixed["o1"]["status"] == "valid" and fixed["o1"]["errors"] == []
    assert fixed["o2"]["status"] == "valid"
    assert selection["ok"] is True
    broken, selection = validate(client, uid, [
        {"id": "o2", "include": True, "data": {"title": "Synthetic dinner", "date": "soon"}},
    ])
    assert broken["o2"]["status"] == "invalid" and broken["o2"]["errors"]
    assert broken["o1"]["status"] == "invalid"
    assert selection["ok"] is False


def test_include_edited_op_applies_and_invalid_include_is_refused(tmp_path, stub):  # noqa: F811
    app, client, uid, _ops = ready(tmp_path, stub)
    refused = client.post(f"/api/capture/{uid}/apply", json={"ops": [
        {"id": "o1", "include": True, "data": {"title": "Synthetic lunch"}},
    ]})
    assert refused.status_code == 400
    error = refused.get_json()["error"]
    assert error["op_id"] == "o1" and error["errors"]
    response = client.post(f"/api/capture/{uid}/apply", json={"ops": [
        {"id": "o1", "include": True, "data": {"title": "Synthetic lunch", "date": "2026-11-05"}},
    ]})
    assert response.status_code == 200, response.get_json()
    conn = app.extensions["hub"].conn()
    try:
        rows = conn.execute(
            "SELECT data FROM entities WHERE type='event' AND status='current'").fetchall()
    finally:
        conn.close()
    titles = sorted(json.loads(row["data"])["title"] for row in rows)
    assert titles == ["Synthetic dinner", "Synthetic lunch"]
