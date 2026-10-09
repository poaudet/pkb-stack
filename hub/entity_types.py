"""Safe optional-property evolution and schema history."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from jsonschema import Draft202012Validator

from . import entities
from .db import tx
from .errors import ApiError, not_found, validation

FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
RESERVED = {
    "id", "uid", "type", "status", "source", "created_at", "updated_at",
    "superseded_by", "status_reason",
}
SENSITIVE = (
    "password", "passwd", "passcode", "passport", "secret", "token", "apikey",
    "api_key", "ssn", "credit", "cardnum", "card_num", "cvv", "iban",
    "account_num", "pin_code", "pincode",
)
SCALARS = {"string", "number", "integer", "boolean"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _type_row(conn, name: str):
    row = conn.execute("SELECT * FROM entity_types WHERE name=?", (name,)).fetchone()
    if row is None:
        raise not_found(f"entity type {name!r} not found")
    return row


def _validate_properties(conn, row, additions: object, max_new: int,
                         schema_override: dict | None = None,
                         version_override: int | None = None) -> dict:
    if not isinstance(additions, dict) or not 1 <= len(additions) <= max_new:
        raise validation(f"add_properties must contain 1..{max_new} properties")
    schema = schema_override or json.loads(row["json_schema"])
    props = schema.get("properties", {})
    if len(props) + len(additions) > 50:
        raise validation("entity type cannot have more than 50 properties")
    added = {}
    folded_names = {name.casefold() for name in props}
    for name, definition in additions.items():
        if not isinstance(name, str) or not FIELD_RE.fullmatch(name):
            raise validation(f"invalid property name {name!r}")
        folded = name.casefold()
        if name in RESERVED or folded in folded_names:
            raise validation(f"reserved or existing property name: {name}")
        if any(blocked in folded for blocked in SENSITIVE):
            raise validation(f"sensitive property name is not allowed: {name}")
        if not isinstance(definition, dict) or set(definition) - {
            "type", "format", "items", "description"
        }:
            raise validation(f"unsupported definition for property {name}")
        kind = definition.get("type")
        description = definition.get("description")
        if not isinstance(description, str) or not 3 <= len(description.strip()) <= 120:
            raise validation(f"{name}: description must be 3-120 characters")
        prop = {"type": kind}
        if kind == "string":
            if "format" in definition:
                if definition["format"] not in ("date", "date-time"):
                    raise validation(f"{name}: unsupported format")
                prop["format"] = definition["format"]
            if "items" in definition:
                raise validation(f"{name}: items only apply to arrays")
        elif kind in ("number", "integer", "boolean"):
            if "format" in definition or "items" in definition:
                raise validation(f"{name}: unsupported definition keyword")
        elif kind == "array":
            items = definition.get("items")
            if (not isinstance(items, dict) or set(items) != {"type"}
                    or items["type"] not in SCALARS):
                raise validation(f"{name}: array items must be scalar")
            if "format" in definition:
                raise validation(f"{name}: arrays cannot have a format")
            prop["items"] = {"type": items["type"]}
        else:
            raise validation(f"{name}: unsupported property type")
        prop["description"] = description.strip()
        added[name] = prop
        folded_names.add(folded)

    proposed = {**schema, "properties": {**props, **added}}
    try:
        Draft202012Validator.check_schema(proposed)
    except Exception as exc:
        raise validation("resulting schema is invalid") from exc

    invalid_count = 0
    for entity_row in conn.execute(
        "SELECT data FROM entities WHERE type=?", (row["name"],)
    ):
        data = json.loads(entity_row["data"])
        for name, prop in added.items():
            value = data.get(name)
            if value is not None and list(Draft202012Validator(
                {"type": prop["type"], **{key: prop[key] for key in ("format", "items")
                                          if key in prop}}
            ).iter_errors(value)):
                invalid_count += 1
                break
    if invalid_count:
        raise ApiError("schema_conflict",
                       f"{invalid_count} existing entities conflict with the new schema", 409,
                       count=invalid_count)
    version = row["schema_version"] if version_override is None else version_override
    return {"schema": proposed, "added": added, "version_before": version,
            "version_after": version + 1}


def preview(conn, name: str, additions: object, max_new: int = 5,
            schema_override: dict | None = None, version_override: int | None = None) -> dict:
    row = _type_row(conn, name)
    return _validate_properties(
        conn, row, additions, max_new, schema_override=schema_override,
        version_override=version_override,
    )


def extend(conn, name: str, add_properties: object, *, actor: str,
           capture_uid: str | None = None, max_new: int | None = None,
           dry_run: bool = False, in_tx: bool = False) -> dict:
    limit = 20 if max_new is None else max_new
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise validation("max_new must be a positive integer")
    if dry_run:
        return preview(conn, name, add_properties, limit)
    def write() -> dict:
        row = _type_row(conn, name)
        preview = _validate_properties(conn, row, add_properties, limit)
        schema = preview["schema"]
        origin = "capture" if capture_uid else "manual"
        for prop in preview["added"].values():
            prop["x-origin"] = origin
            prop["x-added"] = datetime.now(UTC).date().isoformat()
        conn.execute(
            "UPDATE entity_types SET json_schema=?,schema_version=?,updated_at=? WHERE name=?",
            (json.dumps(schema, ensure_ascii=False), preview["version_after"], _now(), name),
        )
        change = {"added": list(preview["added"])}
        conn.execute(
            "INSERT INTO entity_type_history(type,version,json_schema,change,actor,capture_uid) "
            "VALUES(?,?,?,?,?,?)",
            (name, preview["version_after"], json.dumps(schema, ensure_ascii=False),
             json.dumps(change), actor, capture_uid),
        )
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid,detail) "
            "VALUES(?,?,?,?,?)",
            (actor, "type_extend", "entity_type", name,
             json.dumps({"added": list(preview["added"])})),
        )
        return {"type": entities.get_type(conn, name), "version": preview["version_after"],
                "added": list(preview["added"]), "schema": schema,
                "version_before": preview["version_before"]}

    if in_tx:
        return write()
    with tx(conn):
        return write()


def history(conn, name: str) -> dict:
    _type_row(conn, name)
    rows = conn.execute(
        "SELECT version,json_schema,change,actor,capture_uid,at "
        "FROM entity_type_history WHERE type=? ORDER BY version",
        (name,),
    ).fetchall()
    return {"type": name, "history": [{
        "version": row["version"], "json_schema": json.loads(row["json_schema"]),
        "change": json.loads(row["change"]), "actor": row["actor"],
        "capture_uid": row["capture_uid"], "at": row["at"],
    } for row in rows]}


def undo_extension(conn, name: str, added: list[str], version_after: int,
                   capture_uid: str, actor: str, in_tx: bool = False) -> bool:
    def write() -> bool:
        row = _type_row(conn, name)
        if row["schema_version"] != version_after:
            return False
        schema = json.loads(row["json_schema"])
        for entity_row in conn.execute("SELECT data FROM entities WHERE type=?", (name,)):
            data = json.loads(entity_row["data"])
            if any(data.get(field) is not None for field in added):
                return False
        for field in added:
            schema["properties"].pop(field, None)
        version = version_after + 1
        conn.execute(
            "UPDATE entity_types SET json_schema=?,schema_version=?,updated_at=? WHERE name=?",
            (json.dumps(schema, ensure_ascii=False), version, _now(), name),
        )
        change = {"removed": added, "undo_of": capture_uid}
        conn.execute(
            "INSERT INTO entity_type_history(type,version,json_schema,change,actor,capture_uid) "
            "VALUES(?,?,?,?,?,?)",
            (name, version, json.dumps(schema, ensure_ascii=False),
             json.dumps(change), actor, capture_uid),
        )
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid,detail) "
            "VALUES(?,?,?,?,?)",
            (actor, "type_extend_undo", "entity_type", name,
             json.dumps({"removed": added})),
        )
        return True

    if in_tx:
        return write()
    with tx(conn):
        return write()
