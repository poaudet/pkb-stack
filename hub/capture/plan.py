"""Server-side plan normalization, validation, diffs, and safe capture merging."""
from __future__ import annotations

import copy
import json
import logging
import re
from datetime import date, datetime

from jsonschema import Draft202012Validator

from .. import entities, entity_types, notes
from .. import self as self_svc
from ..db import fold
from ..errors import ApiError
from ..policy import capture_excluded_types
from . import names, rules

log = logging.getLogger(__name__)


class OpReject(ValueError):
    """An operation is refused with a machine-readable code and a human message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

ALLOWED = {
    "create_entity", "update_entity", "supersede_entity", "create_entity_type",
    "extend_entity_type",
}
SERVER_ONLY = {
    "include", "status", "errors", "warnings", "diff", "label", "risk", "requires",
    "gap", "source_gap", "reject_reason", "reject_message", "error_code",
}
SELF_STOPLIST = {"i", "me", "myself", "moi", "je", "self", "owner", "user", "mon", "ma"}
REF_STOPLIST = {
    "friend", "friends", "family", "everyone", "someone", "somebody", "people",
    "colleagues", "team", "ami", "amis", "famille", "tout le monde", "quelqu'un",
    "gens", "collegues", "equipe",
}


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


def _inline_refs(value: object, literal: dict[str, str]) -> object:
    if not literal:
        return value
    if isinstance(value, dict):
        if set(value) == {"$ref"} and value["$ref"] in literal:
            return literal[value["$ref"]]
        return {key: _inline_refs(item, literal) for key, item in value.items()}
    if isinstance(value, list):
        return [_inline_refs(item, literal) for item in value]
    return value


def _reject_id_names(type_name: object, values: object) -> None:
    if not names.is_named_type(type_name) or not isinstance(values, dict):
        return
    for key in ("name", "title"):
        if names.is_id_name(values.get(key)):
            raise OpReject("name_is_id", "A name must not be an identifier.")


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
    labels: dict[str, list[dict]] = {}
    for item in candidates:
        if item.get("label"):
            labels.setdefault(fold(item["label"]) or "", []).append(item)
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
                matches = labels.get(fold(value) or "", [])
                candidate = next((item for item in matches if item.get("type") == ref_type), None)
                if candidate and candidate.get("type") == ref_type:
                    rewritten.append(candidate["id"])
                    continue
            rewritten.append(value)
        out[key] = rewritten if is_array else rewritten[0]
    return out


def _type_label_field(type_info: dict) -> str | None:
    props = type_info["json_schema"].get("properties", {})
    for field in (type_info.get("unique_key"), "name", "title"):
        if field and field in props:
            return field
    return None


def _reference_matches(conn, type_name: str, field: str, value: str) -> list[dict]:
    matches = []
    folded = fold(value) or ""
    for row in conn.execute(
        "SELECT uid,data FROM entities WHERE type=? AND status='current' ORDER BY id",
        (type_name,),
    ):
        data = json.loads(row["data"])
        label = data.get(field)
        if (isinstance(label, (str, int, float)) and not isinstance(label, bool)
                and fold(str(label)) == folded):
            matches.append({"id": row["uid"], "label": str(label)})
    return matches


def _resolve_database_refs(conn, data: dict, schema: dict, aliases: dict[str, str],
                           owner: dict | None, types: dict[str, dict]
                           ) -> tuple[dict, list[str], list[str], set[tuple[str, str]]]:
    out = copy.deepcopy(data)
    warnings: list[str] = []
    questions: list[str] = []
    ambiguous: set[tuple[str, str]] = set()
    owner_name = fold(owner["data"].get("name")) if owner else None
    owner_pronouns = SELF_STOPLIST | ({owner_name} if owner_name else set())
    for key, prop in schema.get("properties", {}).items():
        ref_type = prop.get("x-ref")
        if not ref_type or key not in out:
            continue
        type_info = types.get(ref_type)
        label_field = _type_label_field(type_info) if type_info else None
        if not label_field:
            continue
        is_array = prop.get("type") == "array"
        values = out[key] if is_array and isinstance(out[key], list) else [out[key]]
        resolved = []
        for value in values:
            if not isinstance(value, str):
                resolved.append(value)
                continue
            if key == "people" and ref_type == "person" and fold(value) in owner_pronouns:
                warnings.append("self_in_people")
                continue
            if value in aliases:
                resolved.append({"$ref": value})
                continue
            direct = conn.execute(
                "SELECT uid FROM entities WHERE uid=? AND type=? AND status='current'",
                (value, ref_type),
            ).fetchone()
            if direct:
                resolved.append(direct["uid"])
                continue
            matches = _reference_matches(conn, ref_type, label_field, value)
            if len(matches) == 1:
                resolved.append(matches[0]["id"])
            elif len(matches) > 1:
                resolved.append(value)
                labels = ", ".join(item["label"] for item in matches[:3])
                warnings.append(f"ambiguous_ref: {value} ({labels})")
                questions.append(f"Which {ref_type} named {value} did you mean?")
                ambiguous.add((ref_type, fold(value) or ""))
            else:
                resolved.append(value)
        out[key] = resolved if is_array else (resolved[0] if resolved else None)
    return out, warnings, questions, ambiguous


def _gap_label_field(type_info: dict) -> str | None:
    field = _type_label_field(type_info)
    if field is None:
        return None
    required = set(type_info["json_schema"].get("required", []))
    return field if required <= {field} else None


def _fill_reference_gaps(conn, op: dict, data: dict, schema: dict, aliases: dict[str, str],
                         types: dict[str, dict], gaps: dict, settings,
                         blocked: set[tuple[str, str]]) -> dict:
    if not getattr(settings, "capture_gap_fill", True):
        return data
    out = copy.deepcopy(data)
    for field, prop in schema.get("properties", {}).items():
        ref_type = prop.get("x-ref")
        type_info = types.get(ref_type) if ref_type else None
        if type_info is None or ref_type in set(
            getattr(settings, "capture_exclude_types", "place_visit").split(",")
        ):
            continue
        label_field = _gap_label_field(type_info)
        if label_field is None:
            continue
        is_array = prop.get("type") == "array"
        values = out[field] if is_array and isinstance(out[field], list) else [out[field]]
        rewritten = []
        for value in values:
            if not isinstance(value, str):
                rewritten.append(value)
                continue
            folded = fold(value) or ""
            key = (ref_type, folded)
            direct = conn.execute(
                "SELECT 1 FROM entities WHERE uid=? AND type=? AND status='current'",
                (value, ref_type),
            ).fetchone()
            if (key in blocked or value in aliases
                    or direct
                    or len(value) < 2 or len(value) > 60
                    or len(value.split()) > 4
                    or folded in REF_STOPLIST
                    or _reference_matches(conn, ref_type, label_field, value)):
                rewritten.append(value)
                continue
            entry = gaps.get(key)
            if entry is None:
                if len(gaps) >= max(0, getattr(settings, "capture_gap_max", 8)):
                    op["warnings"].append(f"gap limit reached: {value}")
                    rewritten.append(value)
                    continue
                alias = f"g{len(gaps) + 1}"
                gap_op = {
                    "id": alias, "op": "create_entity", "kind": "create_entity",
                    "type": ref_type, "ref": alias, "data": {label_field: value},
                    "status": "valid", "errors": [], "warnings": [], "diff": [],
                    "label": f"Add missing {ref_type} — {value}"[:140],
                    "risk": "auto_gap", "requires": [],
                    "gap": {"kind": "ref", "text": value, "field": field,
                            "needed_by": [op["id"]]},
                    "include": getattr(settings, "capture_gap_default_include", True),
                }
                gaps[key] = {"alias": alias, "op": gap_op}
                aliases[alias] = alias
            else:
                alias = entry["alias"]
                gap_op = entry["op"]
                if op["id"] not in gap_op["gap"]["needed_by"]:
                    gap_op["gap"]["needed_by"].append(op["id"])
            rewritten.append({"$ref": alias})
        out[field] = rewritten if is_array else (rewritten[0] if rewritten else None)
    return out


def _normalized_type_name(name: str) -> str:
    value = re.sub(r"[\s-]+", "_", fold(name) or "")
    if value.endswith("es"):
        value = value[:-2]
    elif value.endswith(("s", "x")):
        value = value[:-1]
    return value


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


def _is_excluded_operation(conn, operation: dict, excluded: set[str],
                           owner_uid: str | None = None) -> bool:
    kind = operation.get("op")
    if not isinstance(kind, str) or kind not in {
        "create_entity", "update_entity", "supersede_entity", "extend_entity_type",
    }:
        return False
    type_name = operation.get("type")
    if isinstance(type_name, str) and type_name in excluded:
        return True
    target = operation.get("target")
    if target == "SELF":
        target = owner_uid
    if isinstance(target, str):
        row = conn.execute("SELECT type FROM entities WHERE uid=?", (target,)).fetchone()
        return bool(row and row["type"] in excluded)
    return False


def assert_no_excluded_operations(conn, raw_plan: dict, settings=None) -> None:
    settings = settings or object()
    excluded = capture_excluded_types(
        conn, getattr(settings, "capture_exclude_types", "")
    )
    owner = self_svc.get_self(conn)["self"]
    owner_uid = owner["id"] if owner else None
    raw_ops = raw_plan.get("ops", []) if isinstance(raw_plan, dict) else []
    if isinstance(raw_ops, list) and any(
        isinstance(op, dict) and _is_excluded_operation(conn, op, excluded, owner_uid)
        for op in raw_ops
    ):
        raise ApiError("type_excluded", "capture operation targets an excluded type", 409)


def with_target_candidates(candidates: list[dict], raw_ops: list) -> list[dict]:
    """Add already-validated update/supersede targets to the candidate set."""
    known = {item.get("id") for item in candidates}
    extra = []
    for op in raw_ops:
        target = op.get("target") if isinstance(op, dict) else None
        if isinstance(target, str) and target not in known and target.casefold() != "self":
            known.add(target)
            extra.append({"id": target})
    return [*candidates, *extra]


def normalize(conn, raw_plan: dict, raw_text: str, candidates: list[dict],
              max_ops: int = 20, settings=None,
              known_refs: dict[str, str] | None = None, note_first: bool = False) -> dict:
    if not isinstance(raw_plan, dict):
        raise TypeError("plan must be a JSON object")
    raw_ops = raw_plan.get("ops")
    if not isinstance(raw_ops, list):
        raise TypeError("ops must be an array")
    keep_note = bool(raw_plan.get("keep_note"))
    if len(raw_ops) + int(keep_note) > max_ops:
        raise ValueError(f"plan has more than {max_ops} operations")
    settings = settings or object()

    all_types = _type_map(conn)
    proposed: dict[str, dict] = {}
    aliases: dict[str, str] = dict(known_refs or {})
    literal_refs: dict[str, str] = {}
    out: list[dict] = []
    warnings: list[str] = []
    questions = raw_plan.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    questions = [q[:300] for q in questions if isinstance(q, str)][:5]
    plan_gaps: list[dict] = []
    owner = self_svc.get_self(conn)["self"]
    seen_ids: set[str] = set()
    type_count = 0
    type_aliases: dict[str, str] = {}
    ref_gaps: dict[tuple[str, str], dict] = {}
    type_op_by_name: dict[str, str] = {}
    extension_op_fields: list[tuple[str, set[str], str]] = []
    extension_previews: dict[int, dict] = {}
    extension_errors: dict[int, str] = {}
    extension_warnings: dict[int, list[str]] = {}
    dropped_extensions: set[int] = set()
    extended_schemas: dict[str, dict] = {}
    extension_versions: dict[str, int] = {}
    max_new_fields = max(1, getattr(settings, "capture_max_new_fields", 5))
    allow_schema = getattr(settings, "capture_allow_schema_changes", True)
    excluded_types = capture_excluded_types(
        conn, getattr(settings, "capture_exclude_types", "")
    )
    owner_uid = owner["id"] if owner else None
    total_new_fields = 0
    extension_count = 0

    for index, candidate_op in enumerate(raw_ops):
        if not isinstance(candidate_op, dict) or candidate_op.get("op") != "extend_entity_type":
            continue
        if _is_excluded_operation(conn, candidate_op, excluded_types, owner_uid):
            dropped_extensions.add(index)
            warnings.append("type_excluded")
            continue
        if not allow_schema:
            dropped_extensions.add(index)
            warnings.append("schema changes disabled; extension dropped")
            continue
        extension_count += 1
        if extension_count > 2:
            extension_errors[index] = "at most two schema extensions are allowed"
            continue
        name = candidate_op.get("type")
        if not isinstance(name, str) or name not in all_types:
            extension_errors[index] = "extension type is unknown or excluded"
            continue
        additions = candidate_op.get("add_properties")
        if not isinstance(additions, dict):
            extension_errors[index] = "add_properties must be an object"
            continue
        remaining = min(3, max_new_fields - total_new_fields)
        if remaining <= 0:
            extension_errors[index] = "CAPTURE_MAX_NEW_FIELDS reached"
            continue
        trimmed = list(additions.items())[:remaining]
        truncated = len(trimmed) != len(additions)
        additions = dict(trimmed)
        try:
            preview = entity_types.preview(
                conn, name, additions, max_new_fields,
                schema_override=extended_schemas.get(name),
                version_override=extension_versions.get(name),
            )
            extension_previews[index] = preview
            extension_warnings[index] = (
                [f"truncated {len(candidate_op['add_properties']) - len(additions)} new fields"]
                if truncated else []
            )
            total_new_fields += len(preview["added"])
            extended_schemas[name] = preview["schema"]
            extension_versions[name] = preview["version_after"]
        except Exception as exc:  # noqa: BLE001 - malformed model operations remain reviewable
            extension_errors[index] = _error_text(exc)

    for name, schema in extended_schemas.items():
        all_types[name]["json_schema"] = schema

    for index, candidate_op in enumerate(raw_ops, start=1):
        if not isinstance(candidate_op, dict):
            warnings.append(f"dropped malformed operation {index}")
            continue
        raw_op = {key: value for key, value in candidate_op.items() if key not in SERVER_ONLY}
        kind = raw_op.get("op")
        if kind not in ALLOWED:
            warnings.append(f"dropped unsupported operation: {str(kind)[:40]}")
            continue
        if index - 1 in dropped_extensions:
            continue
        if _is_excluded_operation(conn, raw_op, excluded_types, owner_uid):
            warnings.append("type_excluded")
            continue
        raw_op, type_reject = rules.map_type(raw_op, all_types)

        forced_error = None
        converted_self = False
        self_alias = False
        if kind == "create_entity" and raw_op.get("type") == "person":
            person_data = raw_op.get("data")
            if isinstance(person_data, dict):
                name = fold(person_data.get("name")) if isinstance(person_data.get("name"), str) else ""
                relationship = (
                    fold(person_data.get("relationship"))
                    if isinstance(person_data.get("relationship"), str) else ""
                )
                if name in SELF_STOPLIST or relationship == "self":
                    if owner is None:
                        forced_error = "self_not_set"
                    else:
                        raw_op = {
                            **raw_op, "op": "update_entity", "target": owner["id"],
                            "patch": {key: value for key, value in person_data.items()
                                      if key not in ("name", "relationship")},
                        }
                        kind = "update_entity"
                        converted_self = True
                        self_alias = True
        if kind in ("update_entity", "supersede_entity"):
            target_uid = raw_op.get("target")
            is_self_target = isinstance(target_uid, str) and (
                target_uid.casefold() == "self" or (owner and target_uid == owner["id"])
            )
            if is_self_target:
                if owner is None:
                    forced_error = "self_not_set"
                else:
                    self_alias = target_uid.casefold() == "self"
                    raw_op["target"] = owner["id"]

        op = _op_base(raw_op, index)
        requested_id = raw_op.get("id")
        if isinstance(requested_id, str) and requested_id and requested_id not in seen_ids:
            op["id"] = requested_id[:40]
        suffix = 1
        while op["id"] in seen_ids:
            op["id"] = f"o{index}_{suffix}"
            suffix += 1
        seen_ids.add(op["id"])
        if converted_self:
            op["status"] = "converted"
            op["warnings"].append("converted to owner update")

        try:
            if type_reject:
                raise OpReject(*type_reject)
            if forced_error:
                if forced_error == "self_not_set":
                    op["status"] = "invalid"
                    op["errors"] = ["self_not_set"]
                    op["error_code"] = "self_not_set"
                    op["reject_reason"] = "self_not_set"
                    op["reject_message"] = "The owner's name is not set yet."
                    op["include"] = False
                    if not plan_gaps:
                        plan_gaps.append({"kind": "self", "action": "set_owner"})
                        questions.append("What name should I use for you?")
                    out.append(op)
                    continue
                raise ValueError(forced_error)
            if kind == "create_entity_type":
                name = raw_op.get("name")
                if not isinstance(name, str) or not entities.NAME_RE.fullmatch(name):
                    raise ValueError("type name is invalid")
                canonical = _normalized_type_name(name)
                equivalent = next((existing for existing in all_types
                                   if _normalized_type_name(existing) == canonical), None)
                if equivalent:
                    type_aliases[name] = equivalent
                    warnings.append(f"type exists: {equivalent}")
                    continue
                equivalent = next((existing for existing in proposed
                                   if _normalized_type_name(existing) == canonical), None)
                if equivalent:
                    type_aliases[name] = equivalent
                    warnings.append(f"type exists: {equivalent}")
                    continue
                if type_count >= 1:
                    raise ValueError("at most one new type is allowed")
                schema = entities.validate_type_schema(raw_op.get("json_schema"))
                desc, template, unique_key, embed = entities._type_fields(raw_op, schema)
                type_count += 1
                proposed[name] = {
                    "name": name, "description": desc, "json_schema": schema,
                    "text_template": template, "unique_key": unique_key, "embed": embed,
                    "builtin": False,
                }
                type_op_by_name[name] = op["id"]
                new_props = set(schema.get("properties", {}))
                similar = []
                for existing_name, info in all_types.items():
                    current_props = set(info["json_schema"].get("properties", {}))
                    union = current_props | new_props
                    overlap = len(current_props & new_props) / len(union) if union else 0
                    if overlap >= 0.5:
                        similar.append((overlap, existing_name))
                op["json_schema"] = schema
                op["description"] = desc
                op["text_template"] = template
                op["unique_key"] = unique_key
                op["embed"] = embed
                op["risk"] = "new_type"
                op["include"] = False
                op["label"] = f"New type — {name}"
                if similar:
                    overlap, existing_name = max(similar)
                    op["risk"] = "similar_type"
                    op["warnings"].append(
                        f"similar to {existing_name}: {round(overlap * 100)}% property overlap"
                    )
                    op["include"] = False
            elif kind == "extend_entity_type":
                preview = extension_previews.get(index - 1)
                if index - 1 in extension_errors:
                    raise ValueError(extension_errors[index - 1])
                if preview is None:
                    raise ValueError("invalid schema extension")
                op["type"] = raw_op["type"]
                op["add_properties"] = {
                    name: {key: value for key, value in definition.items()
                           if key not in ("x-origin", "x-added")}
                    for name, definition in preview["added"].items()
                }
                op["risk"] = "schema_change"
                op["include"] = False
                op["label"] = f"Add fields to {op['type']} — {', '.join(preview['added'])}"[:140]
                op["warnings"].extend(extension_warnings.get(index - 1, []))
                extension_op_fields.append((op["type"], set(preview["added"]), op["id"]))
            elif kind in ("create_entity", "update_entity", "supersede_entity"):
                target = None
                info = None
                if kind == "create_entity":
                    type_name = raw_op.get("type")
                    type_name = type_aliases.get(type_name, type_name)
                    info = all_types.get(type_name) or proposed.get(type_name)
                    if info is None:
                        raise ValueError("unknown entity type")
                    _reject_id_names(type_name, raw_op.get("data"))
                    data, dropped = _clean_data(raw_op.get("data"), _type_schema(info))
                    op["warnings"].extend(dropped)
                    data, ref_warnings, ref_questions, ambiguous = _resolve_database_refs(
                        conn, data, _type_schema(info), aliases, owner, all_types
                    )
                    op["warnings"].extend(ref_warnings)
                    questions.extend(ref_questions)
                    data = _fill_reference_gaps(
                        conn, op, data, _type_schema(info), aliases, all_types, ref_gaps,
                        settings, ambiguous,
                    )
                    data = _inline_refs(data, literal_refs)
                    op["data"] = data
                    ref_errors = [ref for ref in _walk_refs(data) if ref not in aliases]
                    if ref_errors:
                        raise ValueError("reference must name an earlier create op")
                    op["data"] = _replace_ref_values(data, aliases)
                    missing = sorted(set(_type_schema(info).get("required", [])) - set(data))
                    errors = [f"missing: {field}" for field in missing]
                    errors.extend(_json_errors(_type_schema(info), _validation_view(data)))
                    if errors:
                        raise ValueError("; ".join(errors[:5]))
                    duplicate = _find_unique(conn, info, data) if type_name in all_types else None
                    label = next((data[key] for key in ("name", "title")
                                  if isinstance(data.get(key), str)), None)
                    if not duplicate and label and names.is_named_type(type_name):
                        match = next((item for item in candidates
                                      if item.get("type") == type_name
                                      and names.norm_name(item.get("label"))
                                      == names.norm_name(label)), None)
                        if match:
                            extras = {k: v for k, v in data.items() if k not in ("name", "title")}
                            if not extras:
                                if isinstance(raw_op.get("ref"), str) and raw_op["ref"]:
                                    literal_refs[raw_op["ref"]] = match["id"]
                                raise OpReject(
                                    "duplicate_candidate",
                                    "This matches an existing record; the existing one is used.")
                            duplicate = match["id"]
                            data = extras
                    if duplicate:
                        op["kind"] = "update_entity"
                        op["op"] = "update_entity"
                        op["status"] = "converted"
                        op["target"] = duplicate
                        op["patch"] = data
                        op.pop("data", None)
                        op["warnings"].append("already exists")
                        reference = raw_op.get("ref")
                        if isinstance(reference, str) and reference:
                            aliases[reference] = duplicate
                            literal_refs[reference] = duplicate
                        kind = "update_entity"
                    else:
                        reference = raw_op.get("ref")
                        if isinstance(reference, str) and reference:
                            aliases[reference] = op["id"]
                    op["type"] = type_name
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
                    is_owner = uid == (owner or {}).get("id")
                    if is_owner and (
                        not self_alias
                        or rules.has_financial(raw_op.get("patch"), raw_op.get("data"))
                    ):
                        raise OpReject(
                            "owner_record_protected",
                            "Financial or contact details about the owner are never written "
                            "to a record; keep them in a note.")
                    if op["status"] != "converted" and not is_owner and not any(
                        candidate.get("id") == uid for candidate in candidates
                    ):
                        raise OpReject(
                            "target_not_candidate",
                            "The target is not one of the candidate records.")
                    op["type"] = target["type"]
                    op["target"] = uid
                    if kind == "update_entity":
                        patch_input = op.get("patch") if op["status"] == "converted" else raw_op.get("patch")
                        _reject_id_names(target["type"], patch_input)
                        patch, dropped = _clean_data(patch_input, _type_schema(info))
                        op["warnings"].extend(dropped)
                        patch, ref_warnings, ref_questions, ambiguous = _resolve_database_refs(
                            conn, patch, _type_schema(info), aliases, owner, all_types
                        )
                        op["warnings"].extend(ref_warnings)
                        questions.extend(ref_questions)
                        patch = _fill_reference_gaps(
                            conn, op, patch, _type_schema(info), aliases, all_types, ref_gaps,
                            settings, ambiguous,
                        )
                        patch = _inline_refs(patch, literal_refs)
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
                        op["warnings"].extend(_overwrite_warnings(target["data"], merged))
                        op["label"] = (
                            f"Update {target['type']} — "
                            f"{entities.entity_title(target['type'], target['data'])}"
                        )
                        if not op["diff"]:
                            log.info("capture op already recorded: %s", op["id"])
                            continue
                    else:
                        _reject_id_names(target["type"], raw_op.get("data"))
                        data, dropped = _clean_data(raw_op.get("data"), _type_schema(info))
                        op["warnings"].extend(dropped)
                        data, ref_warnings, ref_questions, ambiguous = _resolve_database_refs(
                            conn, data, _type_schema(info), aliases, owner, all_types
                        )
                        op["warnings"].extend(ref_warnings)
                        questions.extend(ref_questions)
                        data = _fill_reference_gaps(
                            conn, op, data, _type_schema(info), aliases, all_types, ref_gaps,
                            settings, ambiguous,
                        )
                        data = _inline_refs(data, literal_refs)
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
                        op["label"] = (
                            f"Supersede {target['type']} — "
                            f"{entities.entity_title(target['type'], target['data'])}"
                        )
            if any(warning.startswith("overwrites_text:") for warning in op["warnings"]):
                op["include"] = False
            out.append(op)
        except OpReject as exc:
            op["status"] = "rejected"
            op["errors"] = [exc.message]
            op["reject_reason"] = exc.code
            op["reject_message"] = exc.message
            op["include"] = False
            op["label"] = str(op.get("label") or kind)[:140]
            out.append(op)
        except Exception as exc:  # noqa: BLE001 - malformed ops remain reviewable
            op["status"] = "invalid"
            op["errors"] = [_error_text(exc)]
            op["reject_reason"] = "invalid_op"
            op["reject_message"] = _error_text(exc)
            op["include"] = False
            op["label"] = str(op.get("label") or kind)[:140]
            out.append(op)

    for entry in ref_gaps.values():
        out.append(entry["op"])

    id_by_reference = {}
    for item in out:
        if item.get("ref"):
            id_by_reference[item["ref"]] = item["id"]
    for item in out:
        if item.get("status") in ("invalid", "rejected"):
            continue
        dependencies = set(item.get("requires", []))
        for reference in _walk_refs(item.get("data", item.get("patch", {}))):
            if reference in id_by_reference and id_by_reference[reference] != item["id"]:
                dependencies.add(id_by_reference[reference])
        if item.get("kind") == "create_entity":
            type_dependency = type_op_by_name.get(item.get("type"))
            if type_dependency:
                dependencies.add(type_dependency)
        affected_fields = set()
        if isinstance(item.get("data"), dict):
            affected_fields.update(item["data"])
        if isinstance(item.get("patch"), dict):
            affected_fields.update(item["patch"])
        for type_name, fields, extension_id in extension_op_fields:
            if item.get("type") == type_name and affected_fields & fields:
                dependencies.add(extension_id)
        dependencies.discard(item["id"])
        item["requires"] = sorted(dependencies)
        if any(not next((dep["include"] for dep in out if dep["id"] == req), True)
               for req in dependencies):
            item["include"] = False

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
        out.append({
            "id": note_id, "op": "create_note", "kind": "create_note",
            "status": "valid", "errors": [], "warnings": [], "diff": [], "include": True,
            "label": f"Keep as note — {title[:100]}", "title": title[:120],
            "tags": [tag[:40] for tag in tags if isinstance(tag, str)][:8], "requires": [],
        })

    order = {
        "create_entity_type": 0, "extend_entity_type": 1, "create_entity": 2,
        "update_entity": 3, "supersede_entity": 4, "create_note": 5,
    }
    remaining = list(out)
    ordered = []
    done: set[str] = set()
    while remaining:
        eligible = [item for item in remaining
                    if set(item.get("requires", [])) <= done]
        if not eligible:
            eligible = [remaining[0]]
        item = min(eligible, key=lambda op: order.get(op["kind"], 9))
        ordered.append(item)
        done.add(item["id"])
        remaining.remove(item)
    if note_first:
        ordered.sort(key=lambda item: item["kind"] != "create_note")
    if not ordered and not questions:
        warnings.append("nothing to record")
    summary = raw_plan.get("summary", "")
    if not isinstance(summary, str):
        summary = ""
    return {
        "summary": summary[:200], "ops": ordered, "keep_note": keep_note,
        "note": {"title": next((item["title"] for item in ordered
                                if item["kind"] == "create_note"), ""),
                 "tags": next((item["tags"] for item in ordered
                               if item["kind"] == "create_note"), [])}
        if keep_note else None,
        "questions": questions[:5], "warnings": warnings, "gaps": plan_gaps,
    }


def plan_schema() -> dict:
    """JSON schema of a model plan; used for structured output and for validation."""
    op_schema = {
        "type": "object",
        "required": ["id", "op"],
        "properties": {
            "id": {"type": "string"},
            "op": {"type": "string", "enum": sorted(ALLOWED)},
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
            "add_properties": {
                "type": "object", "maxProperties": 20,
                "additionalProperties": {
                    "type": "object", "required": ["type", "description"],
                    "properties": {
                        "type": {"enum": ["string", "number", "integer", "boolean", "array"]},
                        "format": {"enum": ["date", "date-time"]},
                        "items": {"type": "object", "properties": {
                            "type": {"enum": ["string", "number", "integer", "boolean"]},
                        }},
                        "description": {"type": "string", "minLength": 3, "maxLength": 120},
                    },
                },
            },
        },
    }
    return {
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


def validate(raw_plan: dict) -> None:
    errors = sorted(Draft202012Validator(plan_schema()).iter_errors(raw_plan),
                    key=lambda error: list(map(str, error.path)))
    if errors:
        raise ValueError("; ".join(error.message for error in errors[:5]))
