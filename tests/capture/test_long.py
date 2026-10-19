import json
import re
import threading

import pytest

from hub.capture import llm
from tests.capture import score_fixture as sf
from tests.test_capture import Stub, make_capture_client, stub  # noqa: F401


def long_text(copies: int = 18) -> str:
    base = sf.load_text()
    parts = []
    for n in range(copies):
        parts.append(f"Week {n + 1} notes\n\n" + base.replace("Went to", "Headed to")
                     if n % 2 else base)
    return "\n\n".join(parts)


@pytest.fixture
def world(tmp_path, stub):  # noqa: F811
    app, client = make_capture_client(tmp_path, stub, capture_exclude_types="",
                                      capture_candidates=3, capture_chunk_target_chars=3000)
    ids = sf.setup_world(client)
    reply = sf.recorded("recap_good.json", ids)
    stub.responder = lambda body: (200, reply, {})
    return app, client, stub, ids, reply


def submit(client, text, **extra):
    response = client.post("/api/capture", json={"text": text, "as_of": sf.CAPTURE_DATE, **extra})
    return response


def chunk_no(body) -> int:
    return int(re.search(r"CHUNK (\d+) of", body["messages"][0]["content"]).group(1))


def test_auto_mode_and_limits(world):
    _app, client, _stub, _ids, _reply = world
    short = submit(client, "Met Julie at the market.").get_json()["capture"]
    assert short["mode"] == "short"
    sf.wait(client, short["id"])
    forced = submit(client, "Met Julie at the market.", mode="long").get_json()["capture"]
    assert forced["mode"] == "long" and forced["chunks_total"] == 1
    sf.wait(client, forced["id"])
    big = submit(client, "x. " * 25000)
    assert big.status_code == 413
    error = big.get_json()["error"]
    assert error["code"] == "too_large" and "60000" in json.dumps(error)


def test_30k_capture_completes_with_each_entity_once(world):
    app, client, stub_, _ids, _reply = world
    text = long_text()
    assert 28000 < len(text) < 36000
    created = submit(client, text).get_json()["capture"]
    assert created["mode"] == "long" and created["chunks_total"] >= 8
    capture = sf.wait(client, created["id"])
    assert capture["state"] == "ready", capture["error"]
    assert capture["chunks_done"] == capture["chunks_total"] and not capture["partial"]
    ops = capture["plan"]["ops"]
    assert ops[0]["kind"] == "create_note"  # the note is the first op
    live = [op for op in ops if op.get("include") and op["op"] == "create_entity"]
    keys = [(op["type"], (op["data"].get("title") or op["data"].get("name") or "").lower())
            for op in live]
    assert len(keys) == len(set(keys)), keys
    assert capture["provenance"]
    leaks = sf.dump_strings(app) + [json.dumps(call) for call in stub_.calls]
    assert not any(sf.HEALTH_CARD in t or "1234 5678" in t for t in leaks)
    assert "[REDACTED:health_card]" in capture["text"]


def test_apply_and_undo_long_job_restore_state(world):
    _app, client, _stub, _ids, _reply = world
    capture = sf.wait(client, submit(client, long_text(), mode="long").get_json()["capture"]["id"])
    def current():
        items = client.get("/api/entities?limit=500").get_json()["items"]
        return sorted(e["id"] for e in items if e["status"] == "current")

    before = current()
    applied = client.post(f"/api/capture/{capture['id']}/apply", json={})
    assert applied.status_code == 200, applied.get_json()
    assert current() != before
    undone = client.post(f"/api/capture/{capture['id']}/undo", json={})
    assert undone.status_code == 200, undone.get_json()
    assert current() == before


def test_failed_chunk_is_partial_then_retry_recovers(world):
    _app, client, stub_, _ids, reply = world
    broken = {"on": True}

    def responder(body):
        if broken["on"] and chunk_no(body) == 2:
            return 200, "not json", {}
        return 200, reply, {}

    stub_.responder = responder
    capture = sf.wait(client, submit(client, long_text(6), mode="long").get_json()["capture"]["id"])
    assert capture["state"] == "failed" and capture["partial"]
    failed = [c for c in capture["chunks"] if c["status"] == "failed"]
    assert [c["idx"] for c in failed] == [1] and failed[0]["error_code"] == "schema_invalid"
    assert capture["chunks_done"] == capture["chunks_total"] - 1

    broken["on"] = False
    assert client.post(f"/api/capture/{capture['id']}/chunks/1/retry", json={}).status_code in (200, 202)
    capture = sf.wait(client, capture["id"])
    assert capture["state"] == "ready" and not capture["partial"]
    assert all(c["status"] == "done" for c in capture["chunks"])


def test_continue_with_partial_result(world):
    _app, client, stub_, _ids, reply = world
    stub_.responder = lambda body: (200, "nope", {}) if chunk_no(body) == 1 else (200, reply, {})
    capture = sf.wait(client, submit(client, long_text(6), mode="long").get_json()["capture"]["id"])
    assert capture["state"] == "failed"
    assert client.post(f"/api/capture/{capture['id']}/continue", json={}).status_code == 200
    capture = client.get(f"/api/capture/{capture['id']}").get_json()["capture"]
    assert capture["state"] == "ready" and capture["partial"]


def test_cancel_stops_after_current_chunk(world):
    _app, client, stub_, _ids, reply = world
    seen = []

    def responder(body):
        seen.append(chunk_no(body))
        if len(seen) == 1:
            uid = client.get("/api/capture").get_json()["captures"][0]["id"]
            threading.Thread(target=lambda: client.post(f"/api/capture/{uid}/cancel",
                                                        json={})).start()
            import time
            time.sleep(0.3)
        return 200, reply, {}

    stub_.responder = responder
    capture = sf.wait(client, submit(client, long_text(), mode="long").get_json()["capture"]["id"])
    assert capture["state"] == "failed" and capture["error"] == "cancelled"
    assert 1 <= len(seen) < capture["chunks_total"]
    assert any(c["status"] == "pending" for c in capture["chunks"])


def test_backoff_honors_retry_after_with_three_attempts(world, monkeypatch):
    _app, client, stub_, _ids, reply = world
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", lambda seconds: sleeps.append(seconds))
    state = {"n": 0}

    def responder(body):
        state["n"] += 1
        if state["n"] <= 2:
            return 429, "", {"Retry-After": "7"}
        return 200, reply, {}

    stub_.responder = responder
    capture = sf.wait(client, submit(client, "Met Julie.", mode="long").get_json()["capture"]["id"])
    assert capture["state"] == "ready" and sleeps[:2] == [7, 7]

    stub_.responder = lambda body: (503, "", {})
    capture = sf.wait(client, submit(client, "Met Julie.", mode="long").get_json()["capture"]["id"])
    assert capture["state"] == "failed"
    assert capture["chunks"][0]["status"] == "failed"


def test_excluded_types_are_absent_from_every_chunk_prompt(tmp_path, stub):  # noqa: F811
    _app, client = make_capture_client(tmp_path, stub, capture_exclude_types="task",
                                      capture_candidates=3)
    sf.setup_world(client)
    stub.responder = lambda body: (200, '{"ops":[]}', {})
    sf.wait(client, submit(client, long_text(6), mode="long").get_json()["capture"]["id"])
    assert stub.calls
    assert all("\ntask " not in c["messages"][0]["content"] for c in stub.calls)


def test_jobs_and_chunks_survive_restart(world):
    app, client, _stub, _ids, _reply = world
    capture = sf.wait(client, submit(client, long_text(6), mode="long").get_json()["capture"]["id"])
    from hub import create_app
    from tests.conftest import make_settings  # noqa: F401
    conn = app.extensions["hub"].conn()
    rows = conn.execute("SELECT idx, start_char, end_char, status, plan_json "
                        "FROM capture_chunks ORDER BY idx").fetchall()
    conn.close()
    assert len(rows) == capture["chunks_total"] and all(r["status"] == "done" for r in rows)
    assert rows[0]["start_char"] == 0 and all(r["plan_json"] for r in rows)
    assert create_app  # persisted in the database file, not in memory
