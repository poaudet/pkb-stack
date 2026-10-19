import pytest

from hub import create_app
from hub.capture import names, plan
from tests.conftest import make_settings


@pytest.fixture
def env(tmp_path):
    app = create_app(make_settings(tmp_path))
    client = app.test_client()
    julie = client.post("/api/entities", json={"type": "person", "data": {"name": "Julie"}})
    tremblay = client.post("/api/entities", json={"type": "person", "data": {"name": "Tremblay"}})
    owner = client.put("/api/self", json={"name": "Taylor"}).get_json()["self"]
    conn = app.extensions["hub"].conn()
    yield conn, {
        "julie": julie.get_json()["entity"]["id"],
        "tremblay": tremblay.get_json()["entity"]["id"], "owner": owner["id"],
    }
    conn.close()


def candidates(ids):
    return [{"id": ids["julie"], "type": "person", "label": "Julie"},
            {"id": ids["tremblay"], "type": "person", "label": "Tremblay"}]


def run(conn, ops, cands):
    return plan.normalize(conn, {"ops": ops}, "text", cands)["ops"]


def test_id_shaped_names_are_rejected(env):
    conn, ids = env
    for value in ("0123456789abcdef0123456789abcdef", "01234567-89ab-cdef-0123-456789abcdef"):
        op = run(conn, [{"id": "a", "op": "create_entity", "type": "person",
                         "data": {"name": value}}], candidates(ids))[0]
        assert op["status"] == "rejected" and op["reject_reason"] == "name_is_id"
        assert op["reject_message"] and op["include"] is False
    assert names.is_id_name("ABCDEF0123456789ABCDEF0123456789")
    assert not names.is_id_name("Julie")


def test_honorific_and_accent_matching_drops_duplicates(env):
    conn, ids = env
    op = run(conn, [{"id": "a", "op": "create_entity", "type": "person",
                     "data": {"name": "Mr. Tremblay"}}], candidates(ids))[0]
    assert op["reject_reason"] == "duplicate_candidate"
    assert names.norm_name("Mme Éloïse  Tremblay") == "eloise tremblay"
    assert names.norm_name("Dr. Julie") == "julie"


def test_references_to_a_dropped_duplicate_resolve_to_the_existing_record(env):
    conn, ids = env
    out = run(conn, [
        {"id": "a", "op": "create_entity", "type": "person", "ref": "t",
         "data": {"name": "Mr. Tremblay"}},
        {"id": "b", "op": "create_entity", "type": "event",
         "data": {"date": "2026-12-06", "title": "Move", "people": [{"$ref": "t"}]}},
    ], candidates(ids))
    assert out[1]["status"] == "valid" and out[1]["data"]["people"] == [ids["tremblay"]]


def test_update_target_must_be_a_candidate(env):
    conn, ids = env
    op = run(conn, [{"id": "a", "op": "update_entity", "target": ids["julie"],
                     "patch": {"notes": "x"}}], [])[0]
    assert op["reject_reason"] == "target_not_candidate" and op["include"] is False
    ok = run(conn, [{"id": "a", "op": "update_entity", "target": ids["julie"],
                     "patch": {"notes": "x"}}], candidates(ids))[0]
    assert ok["status"] == "valid"


def test_owner_record_is_protected_from_ids_and_financial_details(env):
    conn, ids = env
    by_id = run(conn, [{"id": "a", "op": "update_entity", "target": ids["owner"],
                        "patch": {"notes": "hello"}}], candidates(ids))[0]
    assert by_id["reject_reason"] == "owner_record_protected"
    financial = run(conn, [{"id": "a", "op": "update_entity", "target": "SELF",
                            "patch": {"notes": "Car insurance premium is 1,120 dollars"}}],
                    candidates(ids))[0]
    assert financial["reject_reason"] == "owner_record_protected"
    allowed = run(conn, [{"id": "a", "op": "update_entity", "target": "SELF",
                          "patch": {"interests": ["bikes"]}}], candidates(ids))[0]
    assert allowed["status"] == "valid"


def test_bookkeeping_is_not_in_the_review_payload(env, caplog):
    conn, ids = env
    with caplog.at_level("INFO", logger="hub.capture.plan"):
        result = plan.normalize(conn, {"ops": [
            {"id": "a", "op": "update_entity", "target": ids["julie"],
             "patch": {"name": "Julie"}}]}, "text", candidates(ids))
    assert not any("already recorded" in w for w in result["warnings"])
    assert any("already recorded" in r.message for r in caplog.records)
    assert "Julie" not in caplog.text


def test_plan_schema_is_the_validator():
    schema = plan.plan_schema()
    assert schema["required"] == ["ops"]
    with pytest.raises(ValueError):
        plan.validate({"ops": [{"id": "a"}]})
