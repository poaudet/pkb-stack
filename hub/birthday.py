"""Person birthday as MM-DD plus an optional birth_year; legacy full dates are split."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, date, datetime

from . import entities
from .db import tx

log = logging.getLogger(__name__)

MM_DD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
FULL = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?:[T ].*)?$")
UNKNOWN_YEARS = {0, 1900}
PATTERN = r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
BIRTHDAY_PROP = {
    "type": "string", "pattern": PATTERN,
    "description": "Birthday as MM-DD (month and day only, e.g. 04-12). Put the year in birth_year.",
}
YEAR_PROP = {
    "type": "integer", "minimum": 1901, "maximum": 9999,
    "description": "Year of birth, only when known (e.g. 1990).",
}


def _valid_month_day(month: int, day: int) -> bool:
    try:
        date(2000, month, day)
    except ValueError:
        return False
    return True


def split(value: object) -> tuple[str, int | None] | None:
    """Return (MM-DD, year or None) for MM-DD or a full date; None if unparseable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    text = text.removeprefix("--")
    if MM_DD.match(text):
        return (text, None) if _valid_month_day(int(text[:2]), int(text[3:])) else None
    match = FULL.match(text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    if year in UNKNOWN_YEARS:
        return (f"{month:02d}-{day:02d}", None) if _valid_month_day(month, day) else None
    try:
        date(year, month, day)
    except ValueError:
        return None
    return f"{month:02d}-{day:02d}", year


def normalize(data: dict) -> dict:
    """Split a full date written to birthday; drop placeholder birth years."""
    if "birthday" not in data and "birth_year" not in data:
        return data
    out = dict(data)
    parsed = split(out.get("birthday"))
    if parsed:
        out["birthday"] = parsed[0]
        if parsed[1] is not None and out.get("birth_year") is None:
            out["birth_year"] = parsed[1]
    if out.get("birth_year") in UNKNOWN_YEARS:
        out.pop("birth_year")
    return out


def age(data: dict, today: date | None = None) -> int | None:
    """Age in years, only when birth_year is known."""
    year = data.get("birth_year")
    parsed = split(data.get("birthday"))
    if not isinstance(year, int) or isinstance(year, bool) or year in UNKNOWN_YEARS or not parsed:
        return None
    today = today or datetime.now(UTC).date()
    month, day = int(parsed[0][:2]), int(parsed[0][3:])
    return today.year - year - ((today.month, today.day) < (month, day))


def _backup(conn: sqlite3.Connection) -> str | None:
    path = next((row["file"] for row in conn.execute("PRAGMA database_list")
                 if row["name"] == "main"), "")
    if not path:
        return None
    target = f"{path}.pre-birthday-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.bak"
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return target


def migrate(hub, conn: sqlite3.Connection) -> dict | None:
    """Idempotent: move person.birthday to MM-DD + birth_year. Returns a report or None."""
    trow = conn.execute("SELECT * FROM entity_types WHERE name='person'").fetchone()
    if trow is None:
        return None
    schema = json.loads(trow["json_schema"])
    props = schema.get("properties", {})
    if "birthday" not in props:
        return None
    schema_stale = props["birthday"].get("pattern") != PATTERN or "birth_year" not in props
    changes = []
    for row in conn.execute(
        "SELECT id, uid, data, updated_at FROM entities WHERE type='person'"
    ).fetchall():
        data = json.loads(row["data"])
        if "birthday" not in data and "birth_year" not in data:
            continue
        new = normalize(data)
        unparsed = None
        if "birthday" in new and not MM_DD.match(str(new["birthday"])):
            unparsed = new.pop("birthday")
            note = f"Birthday (unparsed): {unparsed}"
            new["notes"] = f"{new['notes']}\n{note}" if new.get("notes") else note
        if new != data:
            changes.append((row, data, new, unparsed))
    if not schema_stale and not changes:
        return None
    if hub.needs_reindex:
        log.warning("birthday migration postponed: embedding reindex required")
        return None

    prepared = []
    new_schema = json.loads(trow["json_schema"])
    new_schema["properties"]["birthday"] = {
        **BIRTHDAY_PROP, **{k: v for k, v in props["birthday"].items() if k.startswith("x-")}}
    new_schema["properties"].setdefault("birth_year", dict(YEAR_PROP))
    for row, _old, new, _unparsed in changes:
        prepared.append(entities._prepare(hub, {**dict(trow), "json_schema": new_schema}, new))
    backup = _backup(conn)
    report = {"backup": backup, "changed": [], "unparseable": []}
    with tx(conn):
        version = trow["schema_version"] + 1
        conn.execute(
            "UPDATE entity_types SET json_schema=?,schema_version=?,updated_at=? WHERE name=?",
            (json.dumps(new_schema, ensure_ascii=False), version, entities._now(), "person"))
        conn.execute(
            "INSERT INTO entity_type_history(type,version,json_schema,change,actor) "
            "VALUES(?,?,?,?,?)",
            ("person", version, json.dumps(new_schema, ensure_ascii=False),
             json.dumps({"changed": ["birthday"], "added": ["birth_year"]}), "migration"))
        for (row, old, new, unparsed), chunk in zip(changes, prepared, strict=True):
            conn.execute(
                "UPDATE entities SET data=?, updated_at=? WHERE id=?",
                (json.dumps(new, ensure_ascii=False),
                 entities._next_updated_at(row["updated_at"]), row["id"]))
            conn.execute(
                "INSERT INTO schema_change_log(type,version,change,field,entity_uid,"
                "removed_value,actor) VALUES(?,?,?,?,?,?,?)",
                ("person", version, "birthday_split", "birthday", row["uid"],
                 json.dumps(old.get("birthday"), ensure_ascii=False), "migration"))
            entities._write_chunk(hub, conn, row["id"], chunk)
            report["unparseable" if unparsed is not None else "changed"].append(row["uid"])
    log.info("birthday migration: %d changed, %d unparseable, backup=%s",
             len(report["changed"]), len(report["unparseable"]), backup)
    return report
