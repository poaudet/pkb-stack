import json

import pytest

from hub import create_app
from hub.capture.redact import redact
from tests.conftest import make_settings

CARD = "ABCD 1234 5678"


@pytest.mark.parametrize("text,kind", [
    (f"my health card is {CARD}.", "health_card"),
    ("health card ABCD-1234-5678-12", "health_card"),
    ("health card ABCD123456789012", "health_card"),
    ("card 4111 1111 1111 1111 expires", "payment_card"),
    ("card 4111-1111-1111-1111", "payment_card"),
    ("SIN 123 456 789", "government_id"),
    ("passport number AB1234567", "government_id"),
    ("driver's licence number D1234-567890-12", "government_id"),
    ("account 123456789012", "bank_account"),
    ("IBAN GB82WEST12345698765432", "bank_account"),
])
def test_patterns_match(text, kind):
    out, counts = redact(text)
    assert counts == {kind: 1}
    assert f"[REDACTED:{kind}]" in out


def test_last_four_and_phrase_kept():
    text = "Visa ending in 4242 and card ending in 1234"
    assert redact(text) == (text, {})


def test_no_false_positives():
    text = (
        "Sunday 18 October 2026, call at 14:00 on 2026-10-22, 12 eggs, 2 lemons, "
        "phone 514-555-1234, renew 2026-11-02 at 9:30, $1,250.00 total, room 4021"
    )
    assert redact(text) == (text, {})


def test_counts_only_no_spans():
    out, counts = redact(f"a {CARD} b {CARD}")
    assert counts == {"health_card": 2}
    assert "1234" not in out


def test_capture_never_stores_card(tmp_path, monkeypatch):
    app = create_app(make_settings(tmp_path, capture_enabled=True, llm_model="m"))
    seen = []
    app.extensions["hub"].capture_queue.submit = lambda uid: seen.append(uid)
    client = app.test_client()
    res = client.post("/api/capture", json={
        "text": f"Insurance {CARD} renews soon", "hint": f"card {CARD}",
    })
    assert res.status_code == 202
    body = res.get_data(as_text=True)
    assert "1234 5678" not in body
    cap = res.get_json()["capture"]
    assert cap["redacted_kinds"] == {"health_card": 1}
    conn = app.extensions["hub"].conn()
    dump = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM captures")])
    conn.close()
    assert "1234 5678" not in dump
    assert "[REDACTED:health_card]" in dump
