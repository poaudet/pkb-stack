"""Scoring harness for the long-capture recap fixture.

Deterministic use (tests): a recorded model response is replayed through the capture
pipeline. Manual use: ``python -m tests.capture.score_fixture --live`` sends the fixture
to the model configured by the CAPTURE_LLM_* / LLM_* environment variables.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hub import create_app
from tests.conftest import make_settings

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
RECAP = FIXTURES / "long_capture_recap.txt"
HEALTH_CARD = "RAMP 1234 5678 90"
CAPTURE_DATE = "2026-10-18"
EXPECTED_TASKS = [
    ("call the mont-royal bike shop", "2026-10-22"),
    ("simplify the retry logic", "2026-10-21"),
    ("renew the staging server certificate", "2026-11-02"),
    ("book a dentist cleaning", "2026-10-31"),
    ("send the quarterly expense report", "2026-10-23"),
    ("review the notes service pull request", "2026-10-20"),
    ("read julie's book about bridges", None),
]
EXPECTED_ITEMS = {
    "oat milk": None, "lemons": 2, "baby spinach": None, "eggs": 12, "basmati rice": None,
    "dish soap": None, "dark roast coffee beans": None,
}
CATEGORIES = ("redaction", "entities", "dates", "types", "groceries", "allergy")
TASK_SCHEMA = {
    "type": "object", "required": ["title"], "properties": {
        "title": {"type": "string"},
        "state": {"type": "string", "enum": ["pending", "done"]},
        "due_date": {"type": "string", "format": "date"},
        "due_time": {"type": "string"}, "notes": {"type": "string"}}}
HABIT_SCHEMA = {
    "type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"}, "cadence": {"type": "string"}}}
LIST_SCHEMA = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
ITEM_SCHEMA = {
    "type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"}, "quantity": {"type": "number"}, "unit": {"type": "string"},
        "list": {"type": "string", "x-ref": "grocery_list"}}}


def load_text() -> str:
    return RECAP.read_text(encoding="utf-8")


def setup_world(client) -> dict:
    """Synthetic owner, existing people and the task/habit/grocery types."""
    for name, schema, key in (
        ("task", TASK_SCHEMA, "title"), ("habit", HABIT_SCHEMA, "name"),
        ("grocery_list", LIST_SCHEMA, "name"), ("grocery_item", ITEM_SCHEMA, "name"),
    ):
        response = client.post("/api/entity-types", json={
            "name": name, "json_schema": schema, "unique_key": key,
            "text_template": "{" + key + "}"})
        assert response.status_code == 201, response.get_json()
    ids = {"owner": client.put("/api/self", json={"name": "Taylor"}).get_json()["self"]["id"]}
    for key, name in (("julie", "Julie"), ("mark", "Mark"), ("tremblay", "Tremblay")):
        response = client.post("/api/entities", json={"type": "person", "data": {"name": name}})
        assert response.status_code == 201, response.get_json()
        ids[key] = response.get_json()["entity"]["id"]
    other = client.post("/api/entities", json={"type": "person", "data": {"name": "Outsider"}})
    ids["outsider"] = other.get_json()["entity"]["id"]
    return ids


def fill(template: str, ids: dict) -> str:
    return (template.replace("{{OWNER_ID}}", ids["owner"])
            .replace("{{OUTSIDER_ID}}", ids["outsider"]))


def recorded(name: str, ids: dict) -> str:
    return fill((FIXTURES / "capture" / name).read_text(encoding="utf-8"), ids)


def _title(op: dict) -> str:
    data = op.get("data") or {}
    return str(data.get("title") or data.get("name") or "")


def _live_ops(plan: dict, normalized: bool) -> list[dict]:
    ops = plan.get("ops", [])
    if not normalized:
        return [op for op in ops if op.get("op") == "create_entity"]
    return [op for op in ops if op.get("include") and op.get("status") not in ("invalid", "rejected")
            and op.get("kind") == "create_entity"]


def score(plan: dict, ids: dict, leaks: list[str], note_text: str,
          normalized: bool = True) -> dict[str, tuple[int, int]]:
    """Return {category: (passed, total)} for a plan."""
    ops = _live_ops(plan, normalized)
    all_ops = plan.get("ops", [])
    result = {name: [0, 0] for name in CATEGORIES}

    def check(category: str, ok: bool) -> None:
        result[category][1] += 1
        result[category][0] += int(ok)

    check("redaction", not any(HEALTH_CARD in text or "1234 5678" in text for text in leaks))
    check("redaction", not any("RAMP" in text and "[REDACTED" not in text for text in leaks))
    people = [op for op in ops if op.get("type") == "person"]
    check("entities", not any(
        len(_title(op)) == 32 and all(c in "0123456789abcdef" for c in _title(op)) for op in ops))
    check("entities", not people)
    check("entities", not any(
        op.get("target") in (ids["owner"], "SELF") and op.get("op") == "update_entity"
        and op.get("include", True) and op.get("status") not in ("invalid", "rejected")
        for op in all_ops))
    allowed = {ids["julie"], ids["mark"], ids["tremblay"]}
    check("entities", not any(
        op.get("target") == ids["outsider"] and op.get("status") not in ("invalid", "rejected")
        and op.get("include", True) and op.get("op") == "update_entity" for op in all_ops)
        and not (allowed - allowed))
    tasks = [op for op in ops if op.get("type") in ("task", "event", "habit")]
    for key, due in EXPECTED_TASKS:
        match = next((op for op in tasks if _title(op).lower().startswith(key)), None)
        check("types", bool(match) and match.get("type") == "task"
              and (match["data"].get("state") == "pending"))
        got = None
        if match:
            data = match["data"]
            got = data.get("due_date") or data.get("date")
        check("dates", bool(match) and got == due)
    market = next((op for op in ops if op.get("type") == "event"
                   and "market" in _title(op).lower()), None)
    check("types", bool(market))
    check("dates", bool(market) and market["data"].get("date") == CAPTURE_DATE)
    check("entities", bool(market) and any(
        isinstance(p, dict) or p == "Julie" or p == ids["julie"]
        for p in (market["data"].get("people") or [])))
    check("types", not any(op.get("type") == "habit" for op in ops))
    check("types", not any(
        "sourdough" in _title(op).lower() or "keyboard" in _title(op).lower()
        for op in ops if op.get("type") in ("task", "event")))
    lists = [op for op in ops if op.get("type") == "grocery_list"]
    items = {_title(op).lower(): op for op in ops if op.get("type") == "grocery_item"}
    check("groceries", len(lists) == 1)
    check("groceries", set(items) == set(EXPECTED_ITEMS))
    for name, quantity in EXPECTED_ITEMS.items():
        got = items.get(name, {}).get("data", {}).get("quantity")
        check("groceries", name in items and got == quantity)
    for name in ("apples", "kale", "cheddar"):
        check("groceries", name not in items)
    check("allergy", "allergic to shellfish" in note_text.lower()
          or any("shellfish" in json.dumps(op.get("data", {})).lower() for op in ops))
    return {name: (a, b) for name, (a, b) in result.items()}


def format_scores(scores: dict[str, tuple[int, int]]) -> str:
    return ", ".join(f"{name} {a}/{b}" for name, (a, b) in scores.items())


def all_pass(scores: dict[str, tuple[int, int]]) -> bool:
    return all(a == b for a, b in scores.values())


def dump_strings(app) -> list[str]:
    """Every stored string that could carry the health card: captures, chunks, notes."""
    conn = app.extensions["hub"].conn()
    try:
        out = []
        for table in ("captures", "capture_chunks", "capture_ops", "notes", "entities",
                      "audit_log"):
            for row in conn.execute(f"SELECT * FROM {table}"):  # fixed names
                out.append(json.dumps([str(v) for v in tuple(row)]))
        return out
    finally:
        conn.close()


def wait(client, uid: str, timeout: float = 30) -> dict:
    until = time.time() + timeout
    while time.time() < until:
        capture = client.get(f"/api/capture/{uid}").get_json()["capture"]
        if capture["state"] in ("ready", "failed"):
            return capture
        time.sleep(0.05)
    raise AssertionError("timed out")


class _Live:
    """Real model passthrough marker used by --live."""


def main(argv: list[str]) -> int:
    live = "--live" in argv
    tmp = tempfile.mkdtemp()
    if live:
        settings = make_settings(
            tmp, capture_llm_base_url=os.environ.get("CAPTURE_LLM_BASE_URL", ""),
            capture_llm_api_key=os.environ.get("CAPTURE_LLM_API_KEY", ""),
            llm_model=os.environ.get("CAPTURE_LLM_MODEL") or os.environ.get("LLM_MODEL", ""))
        app = create_app(settings)
        stub = None
    else:
        stub = _serve(lambda ids: recorded("recap_good.json", ids))
        app = create_app(make_settings(
            tmp, llm_model="stub", capture_llm_base_url=stub[0]))
    client = app.test_client()
    ids = setup_world(client)
    if stub:
        stub[1]["ids"] = ids
    uid = client.post("/api/capture", json={
        "text": load_text(), "mode": "short", "as_of": CAPTURE_DATE}).get_json()["capture"]["id"]
    capture = wait(client, uid)
    leaks = dump_strings(app)
    scores = score(capture["plan"], ids, leaks, capture["text"] or "")
    print(format_scores(scores))
    return 0 if all_pass(scores) else 1


def _serve(reply_for):
    box: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            reply = reply_for(box["ids"])
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "s",
                               "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                   "role": "assistant", "content": reply}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/v1", box


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
