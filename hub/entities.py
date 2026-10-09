"""Entity types and entities: JSON documents validated by JSON Schema."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError

from .db import tx
from .errors import ApiError, not_found, validation

if TYPE_CHECKING:
    from .core import Hub

SEED_PATH = Path(__file__).with_name("seed_types.json")
MAX_SCHEMA_BYTES = 20 * 1024
MAX_DATA_BYTES = 64 * 1024
SCALARS = ("string", "number", "integer", "boolean")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STATUSES = ("current", "superseded", "archived")
OPS = ("eq", "ieq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists", "has")
_CMP = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format(_validator, fmt, instance, _schema):
    if not isinstance(instance, str):
        return
    try:
        if fmt == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", instance):
                raise ValueError
            date.fromisoformat(instance)
        elif fmt == "date-time":
            datetime.fromisoformat(instance)
    except ValueError:
        yield ValidationError(f"value is not a valid {fmt}")


_Validator = validators.extend(Draft202012Validator, {"format": _format})


def _audit(conn, actor: str, action: str, kind: str, uid: str | None, detail: str | None = None):
    conn.execute(
        "INSERT INTO audit_log(actor,action,target_kind,target_uid,detail) VALUES(?,?,?,?,?)",
        (actor, action, kind, uid, detail),
    )


# ---------------------------------------------------------------- types

def _check_refs(node: object) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and not (isinstance(v, str) and v.startswith("#/$defs/")):
                raise validation("$ref must point to #/$defs/...")
            _check_refs(v)
    elif isinstance(node, list):
        for v in node:
            _check_refs(v)


def _check_prop(name: str, prop: object, depth: int) -> None:
    if not isinstance(prop, dict):
        raise validation(f"property {name!r} must be an object")
    if "$ref" in prop:
        return
    t = prop.get("type")
    if t in SCALARS:
        return
    if t == "array":
        items = prop.get("items")
        if not isinstance(items, dict) or not ("$ref" in items or items.get("type") in SCALARS):
            raise validation(f"property {name!r}: arrays must contain scalars")
        return
    if t == "object":
        if depth >= 2:
            raise validation(f"property {name!r}: objects may nest at most 2 levels")
        props = prop.get("properties") or {}
        if not isinstance(props, dict):
            raise validation(f"property {name!r}: properties must be an object")
        for k, v in props.items():
            _check_prop(f"{name}.{k}", v, depth + 1)
        return
    raise validation(f"property {name!r} has an unsupported type")


def validate_type_schema(schema: object) -> dict:
    if not isinstance(schema, dict):
        raise validation("json_schema must be an object")
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        raise validation("json_schema exceeds 20 KB")
    if schema.get("type") != "object":
        raise validation("json_schema must have top-level type 'object'")
    props = schema.get("properties")
    if not isinstance(props, dict) or not 1 <= len(props) <= 50:
        raise validation("json_schema needs 1-50 properties")
    _check_refs(schema)
    for k, v in props.items():
        if not FIELD_RE.match(k):
            raise validation(f"invalid property name {k!r}")
        _check_prop(k, v, 1)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise validation(f"invalid JSON Schema: {e.message}") from e
    return schema


def _type_dict(r: sqlite3.Row) -> dict:
    return {
        "name": r["name"], "description": r["description"],
        "json_schema": json.loads(r["json_schema"]), "text_template": r["text_template"],
        "unique_key": r["unique_key"], "embed": bool(r["embed"]), "builtin": bool(r["builtin"]),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def _type_row(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM entity_types WHERE name=?", (name,)).fetchone()
    if r is None:
        raise not_found(f"entity type {name!r} not found")
    return r


def seed_types(conn: sqlite3.Connection) -> None:
    seeds = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    for name, t in seeds.items():
        conn.execute(
            "INSERT INTO entity_types(name,description,json_schema,text_template,unique_key,"
            "embed,builtin) VALUES(?,?,?,?,?,1,1) ON CONFLICT(name) DO NOTHING",
            (name, t.get("description", ""), json.dumps(t["json_schema"]),
             t.get("text_template"), t.get("unique_key")),
        )
        existing = _type_row(conn, name)
        schema = json.loads(existing["json_schema"])
        changed = False
        for field, definition in t["json_schema"].get("properties", {}).items():
            if (field in schema.get("properties", {})
                    and "description" not in schema["properties"][field]
                    and definition.get("description")):
                schema["properties"][field]["description"] = definition["description"]
                changed = True
        if changed:
            conn.execute(
                "UPDATE entity_types SET json_schema=?,updated_at=? WHERE name=?",
                (json.dumps(schema), _now(), name),
            )


def list_types(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM entity_types ORDER BY builtin DESC, name"):
        d = _type_dict(r)
        d["count"] = conn.execute(
            "SELECT count(*) FROM entities WHERE type=? AND status='current'", (r["name"],)
        ).fetchone()[0]
        out.append(d)
    return out


def get_type(conn: sqlite3.Connection, name: str) -> dict:
    return _type_dict(_type_row(conn, name))


def _type_fields(data: dict, schema: dict) -> tuple[str, str | None, str | None, bool]:
    desc = data.get("description", "")
    tmpl = data.get("text_template")
    ukey = data.get("unique_key")
    if not isinstance(desc, str) or (tmpl is not None and not isinstance(tmpl, str)):
        raise validation("description/text_template must be strings")
    if tmpl is not None and len(tmpl) > 2000:
        raise validation("text_template too long")
    if ukey is not None and ukey not in schema["properties"]:
        raise validation("unique_key must be a top-level property")
    return desc, tmpl, ukey, bool(data.get("embed", True))


def create_type(conn: sqlite3.Connection, data: dict, actor: str = "ui") -> dict:
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise validation("name must match [a-z][a-z0-9_]* (max 40 chars)")
    schema = validate_type_schema(data.get("json_schema"))
    desc, tmpl, ukey, embed = _type_fields(data, schema)
    with tx(conn):
        if conn.execute("SELECT 1 FROM entity_types WHERE name=?", (name,)).fetchone():
            raise ApiError("conflict", "entity type already exists", 409)
        conn.execute(
            "INSERT INTO entity_types(name,description,json_schema,text_template,unique_key,embed)"
            " VALUES(?,?,?,?,?,?)",
            (name, desc, json.dumps(schema), tmpl, ukey, int(embed)),
        )
        _audit(conn, actor, "create", "entity_type", name)
    return get_type(conn, name)


def _is_additive(old: dict, new: dict) -> bool:
    op, np_ = old.get("properties", {}), new.get("properties", {})
    if any(k not in np_ or np_[k] != v for k, v in op.items()):
        return False
    return set(new.get("required", [])) <= set(old.get("required", []))


def patch_type(conn: sqlite3.Connection, name: str, data: dict, force: bool = False,
               actor: str = "ui") -> dict:
    row = _type_row(conn, name)
    cur = _type_dict(row)
    schema = cur["json_schema"]
    if "json_schema" in data:
        schema = validate_type_schema(data["json_schema"])
        if not force and not _is_additive(cur["json_schema"], schema):
            raise ApiError("conflict", "non-additive schema change requires ?force=true", 409)
    merged = {**cur, **{k: data[k] for k in ("description", "text_template", "unique_key", "embed")
                        if k in data}}
    desc, tmpl, ukey, embed = _type_fields(merged, schema)
    with tx(conn):
        conn.execute(
            "UPDATE entity_types SET description=?, json_schema=?, text_template=?, unique_key=?,"
            " embed=?, updated_at=? WHERE name=?",
            (desc, json.dumps(schema), tmpl, ukey, int(embed), _now(), name),
        )
        _audit(conn, actor, "update", "entity_type", name)
    return get_type(conn, name)


def delete_type(conn: sqlite3.Connection, name: str, actor: str = "ui") -> None:
    row = _type_row(conn, name)
    if row["builtin"]:
        raise ApiError("conflict", "built-in types cannot be deleted", 409)
    with tx(conn):
        if conn.execute("SELECT 1 FROM entities WHERE type=? LIMIT 1", (name,)).fetchone():
            raise ApiError("conflict", "entities of this type exist", 409)
        conn.execute("DELETE FROM entity_types WHERE name=?", (name,))
        _audit(conn, actor, "delete", "entity_type", name)


# ------------------------------------------------------------- rendering

def _render_value(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(_render_value(x) for x in v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict):
        return ", ".join(f"{k}: {_render_value(x)}" for k, x in v.items())
    return str(v)


def render_text(type_name: str, template: str | None, data: dict,
                schema: dict | None = None) -> str:
    if template:
        out = re.sub(r"\{(\w+)\}", lambda m: _render_value(data.get(m.group(1))), template)
        out = re.sub(r"[ \t]+", " ", out).strip()
        if schema:
            placeholders = set(re.findall(r"\{(\w+)\}", template))
            extras = []
            length = 0
            for name, prop in schema.get("properties", {}).items():
                value = data.get(name)
                if (name in placeholders or prop.get("type") == "object" or prop.get("x-ref")
                        or value is None or value == "" or value == [] or value == {}):
                    continue
                rendered = _render_value(value)
                addition = f"; {name}: {rendered}"
                remaining = 800 - length
                if remaining <= 0:
                    break
                if len(addition) > remaining:
                    addition = addition[:remaining]
                extras.append(addition)
                length += len(addition)
            out += "".join(extras)
        return out
    parts = "; ".join(f"{k}: {_render_value(v)}" for k, v in data.items())
    return f"{type_name}: {parts}"


def entity_title(type_name: str, data: dict) -> str:
    for k in ("name", "title", "contact"):
        if isinstance(data.get(k), str) and data[k].strip():
            return data[k].strip()[:80]
    for v in data.values():
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    return type_name


# -------------------------------------------------------------- entities

def validate_data(type_row: sqlite3.Row, data: object) -> dict:
    if not isinstance(data, dict):
        raise validation("data must be a JSON object")
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_DATA_BYTES:
        raise ApiError("too_large", "entity data exceeds 64 KB", 413)
    errs = sorted(_Validator(json.loads(type_row["json_schema"])).iter_errors(data),
                  key=lambda e: list(map(str, e.path)))
    if errs:
        msg = "; ".join(
            (".".join(map(str, e.path)) + ": " if e.path else "") + e.message for e in errs[:5]
        )
        raise validation(f"data does not match type schema: {msg}")
    return data


def _uid_of(conn, eid: int | None) -> str | None:
    if eid is None:
        return None
    r = conn.execute("SELECT uid FROM entities WHERE id=?", (eid,)).fetchone()
    return r["uid"] if r else None


def to_dict(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    return {
        "id": r["uid"], "type": r["type"], "data": json.loads(r["data"]), "source": r["source"],
        "status": r["status"], "superseded_by": _uid_of(conn, r["superseded_by"]),
        "status_reason": r["status_reason"], "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _row(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM entities WHERE uid=?", (uid,)).fetchone()
    if r is None:
        raise not_found("entity not found")
    return r


def get_entity(conn: sqlite3.Connection, uid: str) -> dict:
    return to_dict(conn, _row(conn, uid))


def _check_unique(conn, type_row, data: dict, exclude_id: int | None) -> None:
    key = type_row["unique_key"]
    val = data.get(key) if key else None
    if val is None:
        return
    # key is a top-level property name stored on the type (validated at type creation)
    r = conn.execute(
        "SELECT uid FROM entities WHERE type=? AND status='current' AND id IS NOT ? "
        "AND lower(json_extract(data, ?)) = lower(?)",
        (type_row["name"], exclude_id, "$." + key, val),
    ).fetchone()
    if r:
        raise ApiError("conflict", f"a current {type_row['name']} with this {key} exists", 409,
                       existing=r["uid"])


def _prepare(hub: Hub, type_row, data: dict):
    if not type_row["embed"]:
        return None, None, None
    if hub.needs_reindex:
        raise ApiError("needs_reindex", "embedding model changed; reindex required", 409)
    title = entity_title(type_row["name"], data)
    text = render_text(type_row["name"], type_row["text_template"], data,
                       json.loads(type_row["json_schema"]))
    vecs = hub.embedder.embed_documents([f"{title}\n{text}"])
    return title, [text], vecs


def _write_chunk(hub: Hub, conn, eid: int, prepared) -> None:
    from .notes import _write_chunks

    title, chunks, vecs = prepared
    if chunks is None:
        _write_chunks(hub, conn, eid, "", [], [], kind="entity")
    else:
        _write_chunks(hub, conn, eid, title, chunks, vecs, kind="entity")


def _source(actor: str) -> str:
    return actor if actor.startswith("agent:") else "ui" if actor == "ui" else "api"


_PRECOMPUTED_UNSET = object()


def create_entity(hub: Hub, conn, type_name: str, data: object, actor: str = "api", *,
                  uid: str | None = None, precomputed=_PRECOMPUTED_UNSET,
                  in_tx: bool = False, audit: bool = True,
                  owner_internal: bool = False) -> dict:
    trow = _type_row(conn, type_name)
    data = validate_data(trow, data)
    if type_name == "person":
        from . import self as self_svc

        self_svc.assert_change(conn, None, data)
    prepared = _prepare(hub, trow, data) if precomputed is _PRECOMPUTED_UNSET else precomputed
    uid = uid or uuid.uuid4().hex

    def write() -> None:
        _check_unique(conn, trow, data, None)
        eid = conn.execute(
            "INSERT INTO entities(uid,type,data,source) VALUES(?,?,?,?) RETURNING id",
            (uid, type_name, json.dumps(data, ensure_ascii=False), _source(actor)),
        ).fetchone()["id"]
        _write_chunk(hub, conn, eid, prepared)
        if type_name == "person":
            from . import self as self_svc

            self_svc.reconcile(conn)
        if audit:
            _audit(conn, actor, "create", "entity", uid, type_name)

    if in_tx:
        write()
    else:
        with tx(conn):
            write()
    return get_entity(conn, uid)


def merge_patch(target: object, patch: object) -> object:
    """JSON Merge Patch, RFC 7386."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def patch_entity(hub: Hub, conn, uid: str, patch: object, actor: str = "api", *,
                 precomputed=_PRECOMPUTED_UNSET, in_tx: bool = False,
                 audit: bool = True, force: bool = False,
                 owner_internal: bool = False) -> dict:
    row = _row(conn, uid)
    if not isinstance(patch, dict):
        raise validation("patch must be a JSON object")
    trow = _type_row(conn, row["type"])
    data = validate_data(trow, merge_patch(json.loads(row["data"]), patch))
    if row["type"] == "person" and row["status"] == "current":
        from . import self as self_svc

        self_svc.assert_change(conn, row, data, force=force or owner_internal)
    prepared = _prepare(hub, trow, data) if precomputed is _PRECOMPUTED_UNSET else precomputed

    def write() -> None:
        if row["status"] == "current":
            _check_unique(conn, trow, data, row["id"])
        conn.execute("UPDATE entities SET data=?, updated_at=? WHERE id=?",
                     (json.dumps(data, ensure_ascii=False), _now(), row["id"]))
        _write_chunk(hub, conn, row["id"], prepared)
        if row["type"] == "person" and row["status"] == "current":
            from . import self as self_svc

            self_svc.reconcile(conn)
        if audit:
            _audit(conn, actor, "update", "entity", uid)

    if in_tx:
        write()
    else:
        with tx(conn):
            write()
    return get_entity(conn, uid)


def supersede_entity(hub: Hub, conn, uid: str, data: object, reason: str | None = None,
                     actor: str = "api", *, result_uid: str | None = None,
                     precomputed=_PRECOMPUTED_UNSET, in_tx: bool = False,
                     audit: bool = True, force: bool = False) -> dict:
    old = _row(conn, uid)
    if old["status"] != "current":
        raise ApiError("conflict", "only a current entity can be superseded", 409)
    if reason is not None and not isinstance(reason, str):
        raise validation("reason must be a string")
    trow = _type_row(conn, old["type"])
    data = validate_data(trow, data)
    if old["type"] == "person":
        from . import self as self_svc

        self_svc.assert_change(conn, old, data, force=force)
    prepared = _prepare(hub, trow, data) if precomputed is _PRECOMPUTED_UNSET else precomputed
    new_uid = result_uid or uuid.uuid4().hex

    def write() -> None:
        # the old row stops being current first, so a unique_key may be reused by the new one
        conn.execute("UPDATE entities SET status='superseded', status_reason=?, updated_at=? "
                     "WHERE id=?", (reason, _now(), old["id"]))
        _check_unique(conn, trow, data, None)
        nid = conn.execute(
            "INSERT INTO entities(uid,type,data,source) VALUES(?,?,?,?) RETURNING id",
            (new_uid, old["type"], json.dumps(data, ensure_ascii=False), _source(actor)),
        ).fetchone()["id"]
        conn.execute("UPDATE entities SET superseded_by=? WHERE id=?", (nid, old["id"]))
        _write_chunk(hub, conn, nid, prepared)
        if old["type"] == "person":
            from . import self as self_svc

            self_svc.reconcile(conn)
        if audit:
            _audit(conn, actor, "supersede", "entity", uid, new_uid)

    if in_tx:
        write()
    else:
        with tx(conn):
            write()
    return get_entity(conn, new_uid)


def set_status(conn, uid: str, status: str, reason: str | None = None, actor: str = "api",
               force: bool = False) -> dict:
    row = _row(conn, uid)
    if status == "archived" and row["status"] == "archived":
        raise ApiError("conflict", "already archived", 409)
    if status == "current" and row["status"] != "archived":
        raise ApiError("conflict", "only archived entities can be restored", 409)
    if status == "archived" and row["type"] == "person" and row["status"] == "current":
        from . import self as self_svc

        self_svc.assert_change(conn, row, {}, force=force)
    with tx(conn):
        if status == "current":
            _check_unique(conn, _type_row(conn, row["type"]), json.loads(row["data"]), row["id"])
        conn.execute("UPDATE entities SET status=?, status_reason=?, updated_at=? WHERE id=?",
                     (status, reason, _now(), row["id"]))
        _audit(conn, actor, "archive" if status == "archived" else "restore", "entity", uid)
        if row["type"] == "person":
            from . import self as self_svc

            self_svc.reconcile(conn)
    return get_entity(conn, uid)


def forget_entity(hub: Hub, conn, uid: str, actor: str = "api", reason: str | None = None,
                  force: bool = False) -> None:
    row = _row(conn, uid)
    if row["type"] == "person" and row["status"] == "current":
        from . import self as self_svc

        self_svc.assert_change(conn, row, {}, force=force)
    with tx(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM chunks WHERE owner_kind='entity' AND owner_id=?", (row["id"],))]
        hub.store.delete(conn, ids)
        conn.execute("DELETE FROM chunks WHERE owner_kind='entity' AND owner_id=?", (row["id"],))
        conn.execute("DELETE FROM entities WHERE id=?", (row["id"],))
        _audit(conn, actor, "forget", "entity", uid, reason)
        if row["type"] == "person":
            from . import self as self_svc

            self_svc.reconcile(conn)


def list_entities(conn, type_name=None, status=None, limit: int = 50, offset: int = 0) -> dict:
    where, params = [], []
    if type_name:
        where.append("type=?")
        params.append(type_name)
    if status:
        if status not in STATUSES:
            raise validation("invalid status")
        where.append("status=?")
        params.append(status)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT count(*) FROM entities{w}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM entities{w} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset]).fetchall()
    return {"items": [to_dict(conn, r) for r in rows], "total": total}


# ----------------------------------------------------------------- query

def _field(props: dict, name: object) -> str:
    if not isinstance(name, str) or not FIELD_RE.match(name) or name not in props:
        raise validation(f"unknown field {name!r} for this type")
    return "$." + name


def _scalar(v: object) -> object:
    if isinstance(v, (str, int, float)):
        return v
    raise validation("filter value must be a string, number or boolean")


def _like(v: object) -> str:
    s = str(_scalar(v)).lower()
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _filter_sql(props: dict, f: object) -> tuple[str, list]:
    if not isinstance(f, dict):
        raise validation("each filter must be an object")
    path = _field(props, f.get("field"))
    op, val = f.get("op"), f.get("value")
    if op not in OPS:
        raise validation(f"op must be one of {', '.join(OPS)}")
    j = "json_extract(entities.data, ?)"
    if op == "eq":
        return f"{j} = ?", [path, _scalar(val)]
    if op == "ieq":
        return f"lower({j}) = lower(?)", [path, str(_scalar(val))]
    if op == "ne":
        return f"{j} IS NOT ?", [path, _scalar(val)]
    if op in _CMP:
        return f"{j} {_CMP[op]} ?", [path, _scalar(val)]
    if op == "contains":
        return f"lower({j}) LIKE ? ESCAPE '\\'", [path, _like(val)]
    if op == "in":
        if not isinstance(val, list) or not 1 <= len(val) <= 50:
            raise validation("'in' needs a list of 1-50 values")
        return f"{j} IN ({','.join('?' * len(val))})", [path, *[_scalar(v) for v in val]]
    if op == "exists":
        return (f"{j} IS NOT NULL" if val in (None, True) else f"{j} IS NULL"), [path]
    return ("EXISTS (SELECT 1 FROM json_each(entities.data, ?) WHERE lower(value) = lower(?))",
            [path, _scalar(val)])


def query(conn: sqlite3.Connection, q: object) -> dict:
    if not isinstance(q, dict):
        raise validation("JSON object body required")
    tname = q.get("type")
    if not isinstance(tname, str):
        raise validation("type is required")
    props = json.loads(_type_row(conn, tname)["json_schema"])["properties"]
    where, params = ["type=?"], [tname]
    if not q.get("include_superseded"):
        where.append("status='current'")
    filters = q.get("filters") or []
    if not isinstance(filters, list):
        raise validation("filters must be a list")
    for f in filters:
        sql, p = _filter_sql(props, f)
        where.append(sql)
        params += p
    order, oparams = "created_at DESC, id DESC", []
    ob = q.get("order_by")
    if ob:
        if not isinstance(ob, dict):
            raise validation("order_by must be an object")
        direction = str(ob.get("dir", "asc")).lower()
        if direction not in ("asc", "desc"):
            raise validation("order_by.dir must be asc or desc")
        path = _field(props, ob.get("field"))
        # direction is whitelisted above; NULLs sort last in both directions
        order = (f"json_extract(entities.data, ?) IS NULL, json_extract(entities.data, ?) "
                 f"{direction.upper()}, id DESC")
        oparams = [path, path]
    try:
        limit = max(1, min(int(q.get("limit", 50)), 200))
        offset = max(0, int(q.get("offset", 0)))
    except (TypeError, ValueError) as e:
        raise validation("limit/offset must be integers") from e
    w = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) FROM entities WHERE {w}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM entities WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, *oparams, limit, offset]).fetchall()
    return {"items": [to_dict(conn, r) for r in rows], "total": total}
