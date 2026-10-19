import json

import pytest

from tests.capture import score_fixture as sf
from tests.test_capture import make_capture_client, stub  # noqa: F401


@pytest.fixture
def world(tmp_path, stub):  # noqa: F811
    app, client = make_capture_client(tmp_path, stub, capture_exclude_types="", capture_candidates=3)
    return app, client, stub, sf.setup_world(client)


def run(world, response, mode="short"):
    app, client, stub, ids = world  # noqa: F811
    stub.reply = sf.recorded(response, ids)
    uid = client.post("/api/capture", json={
        "text": sf.load_text(), "mode": mode, "as_of": sf.CAPTURE_DATE,
    }).get_json()["capture"]["id"]
    capture = sf.wait(client, uid)
    assert capture["state"] == "ready", capture["error"]
    return capture, ids, app


def test_gate_scores_after_part_a(world):
    capture, ids, app = run(world, "recap_good.json")
    scores = sf.score(capture["plan"], ids, sf.dump_strings(app) + [
        json.dumps(call) for call in world[2].calls], capture["text"])
    assert sf.all_pass(scores), sf.format_scores(scores)


def test_bad_response_is_repaired_by_the_pipeline(world):
    capture, ids, app = run(world, "recap_bad.json")
    reasons = {op.get("reject_reason") for op in capture["plan"]["ops"]}
    assert {"name_is_id", "target_not_candidate", "owner_record_protected",
            "not_a_habit"} <= reasons
    assert all(op.get("reject_message") for op in capture["plan"]["ops"]
               if op["status"] in ("invalid", "rejected"))
    scores = sf.score(capture["plan"], ids, sf.dump_strings(app), capture["text"])
    assert scores["entities"][0] == scores["entities"][1]
    assert scores["redaction"][0] == scores["redaction"][1]


def test_bad_response_scores_poorly_without_the_pipeline(world):
    _capture, ids, _app = run(world, "recap_good.json")
    raw = json.loads(sf.recorded("recap_bad.json", ids))
    scores = sf.score(raw, ids, [json.dumps(raw)], "", normalized=False)
    assert not sf.all_pass(scores)
    assert scores["types"][0] < scores["types"][1]


def test_prompt_has_no_card_no_weekdays_and_resolved_dates(world):
    _capture, _ids, _app = run(world, "recap_good.json")
    user = world[2].calls[0]["messages"][-1]["content"]
    assert "RAMP" not in user and "1234 5678" not in user
    assert "[REDACTED:health_card]" in user
    for word in ("Thursday", "Tuesday", "Wednesday", "Friday", "Sunday"):
        assert word not in user
    assert "2026-10-20" in user and "2026-10-22" in user
    system = world[2].calls[0]["messages"][0]["content"]
    assert "Today is 2026-10-18." in system
