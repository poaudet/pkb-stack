"""Server-side plan normalization, validation, diffs, and safe capture merging."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime

from jsonschema import Draft202012Validator

from .. import entities, notes
from ..db import fold

ALLOWED = {"create_entity", "update_entity", "supersede_entity", "create_entity_type"}


def merge_for_capture(current: object, patch: object) -> object:
    """Merge without deleting: scalars replace, lists union, objects merge recursively."""
    if not isinstance(patch, dict):
        return current if patch is None else patch
    out = dict(current) if isinstance(current, dict) else {}
    for key, value in patch.items():
        if value is None:
            continue
        old = out.get(key)
        if isinstance(value, dict):
            out[key] = merge_for_capture(old, value)
        elif isinstance(value, list):
            items = list(old) if isinstance(old, list) else []
            markers = {
                ("text", fold(item)) if isinstance(item, str)
                else (type(item).__name__, json.dumps(item, sort_keys=True))
                for item in items
            }
            for item in value:
                marker = (("text", fold(item)) if isinstance(item, str)
                          else (type(item).__name__, json.dumps(item, sort_keys=True)))
                if marker not in markers:
                    items.append(item)
                    markers.add(marker)
            out[key] = items
        else:
            out[key] = value
    return out


def _error_text(exc: Exception) -> str:
    message = str(exc)
    return message[:300]


def _json_errors(schema: dict, data: object) -> list[str]:
    errors = sorted(entities._Validator(schema).iter_errors(data),
                    key=lambda error: list(map(str, error.path)))
    return [
        (".".join(map(str, error.path)) + ": " if error.path else "") + error.message
        for error in errors[:5]
    ]


def _coerce(value: object, prop: dict) -> object:
    kind = prop.get("type")
    if kind == "array" and isinstance(value, str) and prop.get("items", {}).get("type") == "string":
        return [part.strip() for part in value.split(",") if part.strip()]
    if kind == "integer" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    if kind == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if kind == "boolean" and isinstance(value, str):
        if value.strip().lower() in ("true", "yes", "1"):
            return True
        if value.strip().lower() in ("false", "no", "0"):
            return False
    if kind == "string" and prop.get("format") == "date" and isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            try:
                return datetime.fromisoformat(value).date().isoformat()
            except ValueError:
                return value
    return value


def _clean_data(data: object, schema: dict) -> tuple[dict, list[str]]:
    if not isinstance(data, dict):
        raise TypeError("data must be an object")
    props = schema.get("properties", {})
    out, warnings = {}, []
    for key, value in data.items():
        if key not in props:
            warnings.append(f"dropped unknown field: {key}")
            continue
        prop = props[key]
        if prop.get("type") == "array" and isinstance(value, list):
            item_schema = prop.get("items", {})
            value = [_coerce(item, item_schema) for item in value]
        else:
            value = _coerce(value, prop)
        out[key] = value
    return out, warnings


def _walk_refs(value: object):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            yield value["$ref"]
        else:
            for child in value.values():
                yield from _walk_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_refs(child)


def _replace_ref_values(value: object, aliases: dict[str, str]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$ref"} and value["$ref"] in aliases:
            return {"$ref": value["$ref"]}
        return {key: _replace_ref_values(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ref_values(item, aliases) for item in value]
    return value


def _validation_view(value: object) -> object:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return "capture-reference"
        return {key: _validation_view(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_validation_view(item) for item in value]
    return value


def _resolve_plain_refs(data: dict, schema: dict, candidates: list[dict],
                        aliases: dict[str, str]) -> dict:
    labels = {fold(item["label"]): item["id"] for item in candidates if item.get("label")}
    out = copy.deepcopy(data)
    for key, prop in schema.get("properties", {}).items():
        ref_type = prop.get("x-ref")
        if not ref_type or key not in out:
            continue
        is_array = prop.get("type") == "array"
        values = out[key] if is_array and isinstance(out[key], list) else [out[key]]
        rewritten = []
        for value in values:
            if isinstance(value, str):
                if value in aliases:
                    rewritten.append({"$ref": value})
                    continue
                uid = labels.get(fold(value))
                candidate = next((item for item in candidates if item.get("id") == uid), None)
                if candidate and candidate.get("type") == ref_type:
                    rewritten.append(uid)
                    continue
            rewritten.append(value)
        out[key] = rewritten if is_array else rewritten[0]
    return out


def _overwrite_warnings(before: object, after: object, path: str = "") -> list[str]:
    warnings = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key, old in before.items():
            if key in after:
                warnings.extend(_overwrite_warnings(old, after[key], f"{path}{key}"))
    elif (isinstance(before, str) and before.strip() and isinstance(after, str)
          and fold(before) not in (fold(after) or "")):
        warnings.append(f"overwrites_text: {path}")
    return warnings


def _diff(before: dict, after: dict) -> list[dict]:
    changes = []
    for key in dict.fromkeys((*before.keys(), *after.keys())):
        if before.get(key) != after.get(key):
            changes.append({"field": key, "old": before.get(key), "new": after.get(key)})
    return changes


def _type_map(conn) -> dict[str, dict]:
    return {item["name"]: item for item in entities.list_types(conn)}


def _type_schema(type_info: dict) -> dict:
    return type_info["json_schema"]


def _find_unique(conn, type_info: dict, data: dict):
    key = type_info.get("unique_key")
    value = data.get(key) if key else None
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        row = conn.execute(
            "SELECT uid FROM entities WHERE type=? AND status='current' "
            "AND fold(json_extract(data, ?))=fold(?) LIMIT 1",
            (type_info["name"], "$." + key, value),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT uid FROM entities WHERE type=? AND status='current' "
            "AND json_extract(data, ?)=? LIMIT 1",
            (type_info["name"], "$." + key, value),
        ).fetchone()
    return row["uid"] if row else None


def _entity(conn, uid: object) -> dict | None:
    if not isinstance(uid, str):
        return None
    row = conn.execute(
        "SELECT uid,type,data,status FROM entities WHERE uid=?", (uid,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row["uid"], "type": row["type"], "data": json.loads(row["data"]),
            "status": row["status"]}


def _op_base(op: dict, ordinal: int) -> dict:
    return {
        **op,
        "id": f"o{ordinal}",
        "kind": op.get("op"),
        "status": "valid",
        "errors": [],
        "warnings": [],
        "diff": [],
        "label": str(op.get("why") or op.get("type") or op.get("name") or op.get("op"))[:140],
        "include": True,
    }


def normalize(conn, raw_plan: dict, raw_text: str, candidates: list[dict],
              max_ops: int = 20) -> dict:
    if not isinstance(raw_plan, dict):
        raise TypeError("plan must be a JSON object")
    raw_ops = raw_plan.get("ops")
    if not isinstance(raw_ops, list):
        raise TypeError("ops must be an array")
    keep_note = bool(raw_plan.get("keep_note"))
    if len(raw_ops) + int(keep_note) > max_ops:
        raise ValueError(f"plan has more than {max_ops} operations")

    all_types = _type_map(conn)
    proposed: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    out: list[dict] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    type_count = 0

    for index, raw_op in enumerate(raw_ops, start=1):
        if not isinstance(raw_op, dict):
            warnings.append(f"dropped malformed operation {index}")
            continue
        kind = raw_op.get("op")
        if kind not in ALLOWED:
            warnings.append(f"dropped unsupported operation: {str(kind)[:40]}")
            continue
        op = _op_base(raw_op, index)
        requested_id = raw_op.get("id")
        if isinstance(requested_id, str) and requested_id and requested_id not in seen_ids:
            op["id"] = requested_id[:40]
        suffix = 1
        while op["id"] in seen_ids:
            op["id"] = f"o{index}_{suffix}"
            suffix += 1
        seen_ids.add(op["id"])

        try:
            if kind == "create_entity_type":
                name = raw_op.get("name")
                if (not isinstance(name, str) or not entities.NAME_RE.fullmatch(name)
                        or name in all_types or name in proposed):
                    raise ValueError("type name is invalid or already exists")
                if type_count >= 2:
                    raise ValueError("at most two new types are allowed")
                schema = entities.validate_type_schema(raw_op.get("json_schema"))
                desc, template, unique_key, embed = entities._type_fields(raw_op, schema)
                type_count += 1
                proposed[name] = {
                    "name": name, "description": desc, "json_schema": schema,
                    "text_template": template, "unique_key": unique_key, "embed": embed,
                    "builtin": False,
                }
                op["json_schema"] = schema
                op["description"] = desc
                op["text_template"] = template
                op["unique_key"] = unique_key
                op["embed"] = embed
                op["risk"] = "new_type"
                op["include"] = False
                op["label"] = f"New type — {name}"
            elif kind in ("create_entity", "update_entity", "supersede_entity"):
                target = None
                info = None
                if kind == "create_entity":
                    type_name = raw_op.get("type")
                    info = all_types.get(type_name) or proposed.get(type_name)
                    if info is None:
                        raise ValueError("unknown entity type")
                    data, dropped = _clean_data(raw_op.get("data"), _type_schema(info))
                    op["warnings"].extend(dropped)
                    op["data"] = data
                    if kind == "create_entity":
                        data = _resolve_plain_refs(data, _type_schema(info), candidates, aliases)
                        op["data"] = data
                    ref_errors = [ref for ref in _walk_refs(data) if ref not in aliases]
                    if ref_errors:
                        raise ValueError("reference must name an earlier create op")
                    op["data"] = _replace_ref_values(data, aliases)
                    missing = sorted(set(_type_schema(info).get("required", [])) - set(data))
                    errors = [f"missing: {name}" for name in missing]
                    errors.extend(_json_errors(_type_schema(info), _validation_view(data)))
                    if errors:
                        raise ValueError("; ".join(errors[:5]))
                    duplicate = _find_unique(conn, info, data) if type_name in all_types else None
                    if duplicate:
                        op["kind"] = "update_entity"
                        op["op"] = "update_entity"
                        op["status"] = "converted"
                        op["target"] = duplicate
                        op["patch"] = data
                        op.pop("data", None)
                        op["warnings"].append("already exists")
                        ref = raw_op.get("ref")
                        if isinstance(ref, str) and ref:
                            aliases[ref] = duplicate
                        kind = "update_entity"
                    else:
                        ref = raw_op.get("ref")
                        if isinstance(ref, str) and ref:
                            aliases[ref] = op["id"]
                if kind in ("update_entity", "supersede_entity"):
                    uid = op.get("target") if op["status"] == "converted" else raw_op.get("target")
                    target = _entity(conn, uid)
                    if target is None:
                        raise ValueError("target entity does not exist")
                    if target["status"] != "current":
                        raise ValueError("target entity is not current")
                    info = all_types.get(target["type"])
                    if info is None:
                        raise ValueError("target entity type does not exist")
                    if not any(candidate.get("id") == uid for candidate in candidates):
                        op["warnings"].append("target was not in candidate context")
                    op["type"] = target["type"]
                    op["target"] = uid
                    if kind == "update_entity":
                        patch_input = op.get("patch") if op["status"] == "converted" else raw_op.get("patch")
                        patch, dropped = _clean_data(patch_input, _type_schema(info))
                        op["warnings"].extend(dropped)
                        patch = _resolve_plain_refs(patch, _type_schema(info), candidates, aliases)
                        ref_errors = [ref for ref in _walk_refs(patch) if ref not in aliases]
                        if ref_errors:
                            raise ValueError("reference must name an earlier create op")
                        patch = _replace_ref_values(patch, aliases)
                        merged = merge_for_capture(target["data"], patch)
                        errors = _json_errors(_type_schema(info), _validation_view(merged))
                        if errors:
                            raise ValueError("; ".join(errors[:5]))
                        op["patch"] = patch
                        op["diff"] = _diff(target["data"], merged)
                        op["warnings"].extend(
                            _overwrite_warnings(target["data"], merged)
                        )
                        op["label"] = f"Update {target['type']} — {entities.entity_title(target['type'], target['data'])}"
                        if not op["diff"]:
                            warnings.append(f"{op['id']}: already recorded")
                            continue
                    else:
                        data, dropped = _clean_data(raw_op.get("data"), _type_schema(info))
                        op["warnings"].extend(dropped)
                        data = _resolve_plain_refs(data, _type_schema(info), candidates, aliases)
                        ref_errors = [ref for ref in _walk_refs(data) if ref not in aliases]
                        if ref_errors:
                            raise ValueError("reference must name an earlier create op")
                        data = _replace_ref_values(data, aliases)
                        errors = _json_errors(_type_schema(info), _validation_view(data))
                        reason = raw_op.get("reason")
                        if not isinstance(reason, str) or not reason.strip():
                            errors.append("reason is required")
                        if errors:
                            raise ValueError("; ".join(errors[:5]))
                        op["data"] = data
                        op["reason"] = reason
                        op["diff"] = _diff(target["data"], data)
                        op["label"] = f"Supersede {target['type']} — {entities.entity_title(target['type'], target['data'])}"
            if any(warning.startswith("overwrites_text:") for warning in op["warnings"]):
                op["include"] = False
            out.append(op)
        except Exception as exc:  # noqa: BLE001 - malformed ops become editable invalid plan entries
            op["status"] = "invalid"
            op["errors"] = [_error_text(exc)]
            op["include"] = False
            op["label"] = str(op.get("label") or kind)[:140]
            out.append(op)

    # Type definitions precede entities; preserve model order within each kind.
    types = [op for op in out if op["kind"] == "create_entity_type"]
    creates = [op for op in out if op["kind"] == "create_entity"]
    updates = [op for op in out if op["kind"] == "update_entity"]
    supersedes = [op for op in out if op["kind"] == "supersede_entity"]
    note = raw_plan.get("note") if isinstance(raw_plan.get("note"), dict) else {}
    if keep_note:
        title = note.get("title")
        tags = note.get("tags", [])
        if not isinstance(title, str):
            title = notes.default_title(raw_text)
        if not isinstance(tags, list):
            tags = []
        note_id = f"o{len(raw_ops) + 1}"
        while note_id in seen_ids:
            note_id = "note_" + note_id
        note_op = {
            "id": note_id, "op": "create_note", "kind": "create_note",
            "status": "valid", "errors": [], "warnings": [], "diff": [], "include": True,
            "label": f"Keep as note — {title[:100]}", "title": title[:120],
            "tags": [tag[:40] for tag in tags if isinstance(tag, str)][:8],
        }
        out.append(note_op)
    ordered = types + creates + updates + supersedes + [op for op in out if op["kind"] == "create_note"]
    questions = raw_plan.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    questions = [q[:300] for q in questions if isinstance(q, str)][:5]
    if not ordered and not questions:
        warnings.append("nothing to record")
    summary = raw_plan.get("summary", "")
    if not isinstance(summary, str):
        summary = ""
    return {
        "summary": summary[:200],
        "ops": ordered,
        "keep_note": keep_note,
        "note": {"title": ordered[-1]["title"], "tags": ordered[-1]["tags"]}
        if keep_note else None,
        "questions": questions,
        "warnings": warnings,
    }


def validate(raw_plan: dict) -> None:
    op_schema = {
        "type": "object",
        "required": ["id", "op"],
        "properties": {
            "id": {"type": "string"},
            "op": {"type": "string"},
            "type": {"type": "string"},
            "name": {"type": "string"},
            "ref": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,30}$"},
            "target": {"type": "string"},
            "data": {"type": "object"},
            "patch": {"type": "object"},
            "reason": {"type": "string"},
            "why": {"type": "string"},
            "description": {"type": "string"},
            "json_schema": {"type": "object"},
            "text_template": {"type": "string"},
            "unique_key": {"type": "string"},
        },
    }
    schema = {
        "type": "object",
        "required": ["ops"],
        "properties": {
            "summary": {"type": "string", "maxLength": 200},
            "keep_note": {"type": "boolean"},
            "note": {"type": ["object", "null"], "properties": {
                "title": {"type": "string", "maxLength": 120},
                "tags": {"type": "array", "maxItems": 8,
                         "items": {"type": "string", "maxLength": 40}},
            }},
            "questions": {"type": "array", "maxItems": 5,
                          "items": {"type": "string", "maxLength": 300}},
            "ops": {"type": "array", "maxItems": 20, "items": op_schema},
        },
    }
    errors = sorted(Draft202012Validator(schema).iter_errors(raw_plan),
                    key=lambda error: list(map(str, error.path)))
    if errors:
        raise ValueError("; ".join(error.message for error in errors[:5]))
