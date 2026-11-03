from __future__ import annotations

import glob
import json
from datetime import date

from hub import birthday


def _create(client, data):
    return client.post("/api/entities", json={"type": "person", "data": data})


def test_split_and_age():
    assert birthday.split("1990-04-12") == ("04-12", 1990)
    assert birthday.split("0000-04-12") == ("04-12", None)
    assert birthday.split("1900-04-12") == ("04-12", None)
    assert birthday.split("02-29") == ("02-29", None)
    assert birthday.split("2001-02-29") is None
    assert birthday.split("soon") is None
    assert birthday.age({"birthday": "04-12", "birth_year": 1990}, date(2026, 4, 11)) == 35
    assert birthday.age({"birthday": "04-12", "birth_year": 1990}, date(2026, 4, 12)) == 36
    assert birthday.age({"birthday": "04-12"}, date(2026, 4, 12)) is None


def test_full_date_is_split_on_create_and_patch(client):
    r = _create(client, {"name": "Ann", "birthday": "1990-04-12"})
    assert r.status_code == 201, r.get_json()
    data = r.get_json()["entity"]["data"]
    assert data["birthday"] == "04-12" and data["birth_year"] == 1990
    r = _create(client, {"name": "Bo", "birthday": "1900-05-06"})
    data = r.get_json()["entity"]["data"]
    assert data["birthday"] == "05-06" and "birth_year" not in data
    uid = r.get_json()["entity"]["id"]
    patched = client.patch(f"/api/entities/{uid}", json={"birthday": "1985-06-07"})
    assert patched.status_code == 200, patched.get_json()
    assert patched.get_json()["entity"]["data"]["birth_year"] == 1985
    assert _create(client, {"name": "Cy", "birthday": "nonsense"}).status_code == 400


def test_migration_is_idempotent_backs_up_and_reports(tmp_path, mk):
    app = mk()
    hub = app.extensions["hub"]
    conn = hub.conn()
    legacy = {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"},
        "birthday": {"type": "string", "format": "date"},
        "notes": {"type": "string"}}}
    conn.execute("UPDATE entity_types SET json_schema=? WHERE name='person'", (json.dumps(legacy),))
    for uid, data in (("a" * 32, {"name": "Old", "birthday": "1980-03-04"}),
                      ("b" * 32, {"name": "Placeholder", "birthday": "0000-07-08"}),
                      ("c" * 32, {"name": "Weird", "birthday": "1990-99-99"}),
                      ("d" * 32, {"name": "None"})):
        conn.execute("INSERT INTO entities(uid,type,data,source) VALUES(?,?,?,?)",
                     (uid, "person", json.dumps(data), "api"))
    conn.close()

    app2 = mk()
    hub2 = app2.extensions["hub"]
    conn = hub2.conn()
    try:
        rows = {r["uid"]: json.loads(r["data"]) for r in conn.execute(
            "SELECT uid,data FROM entities WHERE type='person'")}
        assert rows["a" * 32] == {"name": "Old", "birthday": "03-04", "birth_year": 1980}
        assert rows["b" * 32] == {"name": "Placeholder", "birthday": "07-08"}
        assert "birthday" not in rows["c" * 32]
        assert "1990-99-99" in rows["c" * 32]["notes"]
        assert rows["d" * 32] == {"name": "None"}
        props = json.loads(conn.execute(
            "SELECT json_schema FROM entity_types WHERE name='person'").fetchone()[0])["properties"]
        assert "birth_year" in props and "pattern" in props["birthday"]
        assert conn.execute("SELECT count(*) FROM schema_change_log").fetchone()[0] == 3
        before = conn.execute("SELECT schema_version FROM entity_types WHERE name='person'"
                              ).fetchone()[0]
    finally:
        conn.close()
    assert glob.glob(str(tmp_path / "**" / "*.pre-birthday-*.bak"), recursive=True)

    assert birthday.migrate(hub2, hub2.conn()) is None
    conn = hub2.conn()
    try:
        assert conn.execute("SELECT schema_version FROM entity_types WHERE name='person'"
                            ).fetchone()[0] == before
    finally:
        conn.close()
