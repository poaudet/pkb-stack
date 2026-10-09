"""Atomic capture plan application and conditional undo."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from .. import entities, entity_types, notes
from .. import self as self_svc
from ..db import tx
from ..errors import ApiError
from . import context, jobs, plan


def _hash_data(data: dict) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve(value: object, refs: dict[str, str]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            alias = value["$ref"]
            if alias not in refs:
                raise ValueError("unresolved plan reference")
            return refs[alias]
        return {key: _resolve(item, refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, refs) for item in value]
    return value


def _plan_input(stored: dict, body: dict) -> tuple[dict, dict[str, dict]]:
    updates: dict[str, dict] = {}
    raw_ops = []
    for op in stored.get("ops", []):
        if op.get("kind") == "create_note":
            continue
        updates[op.get("id")] = {"id": op.get("id"), "include": op.get("include", True)}
        raw = {key: op[key] for key in (
            "id", "op", "type", "name", "ref", "target", "data", "patch", "reason",
            "description", "json_schema", "text_template", "unique_key", "embed",
            "add_properties",
        ) if key in op}
        raw_ops.append(raw)
    overrides = body.get("ops", [])
    if not isinstance(overrides, list):
        raise ApiError("validation_error", "ops must be an array", 400)
    for override in overrides:
        if not isinstance(override, dict) or not isinstance(override.get("id"), str):
            raise ApiError("validation_error", "each op override needs an id", 400)
        if set(override) - {"id", "include", "data", "patch", "add_properties"}:
            raise ApiError("validation_error", "unsupported op override field", 400)
        if "include" in override and not isinstance(override["include"], bool):
            raise ApiError("validation_error", "include must be a boolean", 400)
        if any(key in override and not isinstance(override[key], dict)
               for key in ("data", "patch", "add_properties")):
            raise ApiError(
                "validation_error", "data, patch, and add_properties overrides must be objects",
                400,
            )
        if override["id"] not in updates:
            raise ApiError("validation_error", "unknown operation id", 400)
        target = next(op for op in raw_ops if op.get("id") == override["id"])
        allowed_for_kind = {
            "create_entity": {"data"},
            "supersede_entity": {"data"},
            "update_entity": {"patch"},
            "extend_entity_type": {"add_properties"},
        }.get(target.get("op"), set())
        if set(override) - {"id", "include"} - allowed_for_kind:
            raise ApiError("validation_error", "override does not match operation kind", 400)
        updates[override["id"]].update(override)
    for op in raw_ops:
        override = updates.get(op.get("id"), {})
        for key in ("data", "patch", "add_properties"):
            if key in override:
                op[key] = override[key]
    keep_note = body.get("keep_note", stored.get("keep_note", False))
    if not isinstance(keep_note, bool):
        raise ApiError("validation_error", "keep_note must be a boolean", 400)
    note = stored.get("note")
    raw_plan = {
        "summary": stored.get("summary", ""),
        "ops": raw_ops,
        "keep_note": keep_note,
        "note": note,
        "questions": stored.get("questions", []),
    }
    return raw_plan, updates


def _entity_data(conn, uid: str) -> tuple[dict, dict]:
    row = conn.execute(
        "SELECT * FROM entities WHERE uid=?", (uid,)
    ).fetchone()
    if row is None:
        raise ApiError("capture_apply", "entity no longer exists", 409)
    return dict(row), json.loads(row["data"])


def apply(hub, conn, uid: str, body: dict, actor: str) -> dict:
    capture_row = jobs._row(conn, uid)
    if capture_row["state"] != "ready":
        raise ApiError("capture_state", "capture must be ready before apply", 409)
    if not isinstance(body, dict):
        raise ApiError("validation_error", "JSON object body required", 400)
    stored = json.loads(capture_row["plan"])
    raw_plan, overrides = _plan_input(stored, body)
    if "allow_new_types" in body and not isinstance(body["allow_new_types"], bool):
        raise ApiError("validation_error", "allow_new_types must be a boolean", 400)
    if set(body) - {"ops", "keep_note", "allow_new_types"}:
        raise ApiError("validation_error", "unsupported apply field", 400)
    raw_text = capture_row["raw_text"] or ""
    candidates = context.build_candidates(
        hub, conn, raw_text, hub.settings.capture_candidates
    ) if raw_text else []
    normalized = plan.normalize(
        conn, raw_plan, raw_text, candidates,
        max_ops=max(1, min(20, hub.settings.capture_max_ops)),
        settings=hub.settings,
    )
    for item in normalized["ops"]:
        override = overrides.get(item["id"], {})
        if "include" in override:
            item["include"] = override["include"]
        if item["kind"] == "create_entity_type" and item["include"] and not body.get(
            "allow_new_types", False
        ):
            raise ApiError("validation_error", "allow_new_types is required", 400,
                           op_id=item["id"])
        if item["include"] and item["status"] == "invalid":
            raise ApiError("validation_error", "included operation is invalid", 400,
                           op_id=item["id"], errors=item["errors"])

    selected = [op for op in normalized["ops"] if op["include"]]
    selected_ids = {op["id"] for op in selected}
    missing = [
        {"op": op["id"], "requires": [dependency for dependency in op.get("requires", [])
                                      if dependency not in selected_ids]}
        for op in selected if any(dependency not in selected_ids
                                  for dependency in op.get("requires", []))
    ]
    if missing:
        raise ApiError("dependency", "selected operations require unchecked operations", 409,
                       missing=missing)

    prepared: dict[str, dict] = {}
    uid_by_op: dict[str, str] = {}
    refs: dict[str, str] = {}
    for op in selected:
        if op["kind"] in ("create_entity", "supersede_entity"):
            result_uid = uuid.uuid4().hex
            uid_by_op[op["id"]] = result_uid
            if op.get("ref"):
                refs[op["ref"]] = result_uid

    proposed_types = {
        op["name"]: {
            "name": op["name"], "description": op["description"],
            "json_schema": op["json_schema"], "text_template": op.get("text_template"),
            "unique_key": op.get("unique_key"), "embed": op.get("embed", True),
        }
        for op in selected if op["kind"] == "create_entity_type"
    }
    extension_schemas: dict[str, dict] = {}
    extension_versions: dict[str, int] = {}
    extension_previews: dict[str, dict] = {}
    for op in selected:
        if op["kind"] != "extend_entity_type":
            continue
        preview = entity_types.preview(
            conn, op["type"], op["add_properties"],
            max(1, hub.settings.capture_max_new_fields),
            schema_override=extension_schemas.get(op["type"]),
            version_override=extension_versions.get(op["type"]),
        )
        extension_previews[op["id"]] = preview
        extension_schemas[op["type"]] = preview["schema"]
        extension_versions[op["type"]] = preview["version_after"]
    extended_types = {}
    for type_name, schema in extension_schemas.items():
        type_info = entities.get_type(conn, type_name)
        type_info["json_schema"] = schema
        extended_types[type_name] = type_info

    available_types = set(proposed_types)
    unavailable_types = {
        op["type"] for op in selected if op["kind"] == "create_entity"
        and op["type"] not in available_types
    } - {row["name"] for row in entities.list_types(conn)}
    if unavailable_types:
        raise ApiError("validation_error", "a selected create uses an unchecked new type", 400,
                       types=sorted(unavailable_types))

    for op in selected:
        kind = op["kind"]
        if kind == "create_entity":
            data = _resolve(op["data"], refs)
            type_info = (
                proposed_types.get(op["type"]) or extended_types.get(op["type"])
                or entities.get_type(conn, op["type"])
            )
            prepared[op["id"]] = {
                "data": data, "entity": entities._prepare(hub, type_info, data),
            }
        elif kind == "update_entity":
            row, before = _entity_data(conn, op["target"])
            if row["status"] != "current":
                raise ApiError("capture_apply", "update target is no longer current", 409,
                               op_id=op["id"])
            patch_data = _resolve(op["patch"], refs)
            final = plan.merge_for_capture(before, patch_data)
            type_info = extended_types.get(row["type"]) or entities.get_type(conn, row["type"])
            type_row = conn.execute(
                "SELECT * FROM entity_types WHERE name=?", (row["type"],)
            ).fetchone()
            if row["type"] in extension_schemas:
                type_row = dict(type_row)
                type_row["json_schema"] = json.dumps(extension_schemas[row["type"]])
            final = entities.validate_data(
                type_row, final,
            )
            prepared[op["id"]] = {
                "before": before, "before_hash": _hash_data(before), "final": final,
                "entity": entities._prepare(hub, type_info, final),
            }
        elif kind == "supersede_entity":
            row, before = _entity_data(conn, op["target"])
            if row["status"] != "current":
                raise ApiError("capture_apply", "supersede target is no longer current", 409,
                               op_id=op["id"])
            final = _resolve(op["data"], refs)
            type_info = extended_types.get(row["type"]) or entities.get_type(conn, row["type"])
            type_row = conn.execute(
                "SELECT * FROM entity_types WHERE name=?", (row["type"],)
            ).fetchone()
            if row["type"] in extension_schemas:
                type_row = dict(type_row)
                type_row["json_schema"] = json.dumps(extension_schemas[row["type"]])
            final = entities.validate_data(type_row, final)
            prepared[op["id"]] = {
                "before": before, "before_hash": _hash_data(before), "final": final,
                "entity": entities._prepare(hub, type_info, final),
            }
        elif kind == "create_note":
            raw = capture_row["raw_text"]
            if raw is None:
                raise ApiError("capture_state", "capture text retention has expired", 409)
            content = notes._check_content(raw)
            title = notes._check_title(op.get("title"), content)
            tags = notes._clean_tags(op.get("tags"))
            happened = None
            chunks, vecs = notes._prepare(hub, title, content)
            prepared[op["id"]] = {
                "title": title, "content": content, "tags": tags,
                "happened_at": happened, "chunks": chunks, "vecs": vecs,
            }

    applied: list[dict] = []
    skipped = [op["id"] for op in normalized["ops"] if not op["include"]]
    mirrored_notes: list[str] = []
    active_op = "?"
    try:
        with tx(conn):
            for op in selected:
                active_op = op["id"]
                kind = op["kind"]
                item = prepared.get(op["id"], {})
                if kind == "create_entity_type":
                    conn.execute(
                        "INSERT INTO entity_types(name,description,json_schema,text_template,"
                        "unique_key,embed) VALUES(?,?,?,?,?,?)",
                        (op["name"], op["description"], json.dumps(op["json_schema"]),
                         op.get("text_template"), op.get("unique_key"),
                         int(op.get("embed", True))),
                    )
                    conn.execute(
                        "INSERT INTO entity_type_history(type,version,json_schema,change,actor) "
                        "VALUES(?,1,?,'{}',?)",
                        (op["name"], json.dumps(op["json_schema"]), actor),
                    )
                    entity_type, target_uid, result_uid = op["name"], None, op["name"]
                elif kind == "extend_entity_type":
                    extension = entity_types.extend(
                        conn, op["type"], op["add_properties"], actor=actor,
                        capture_uid=uid, max_new=max(1, hub.settings.capture_max_new_fields),
                        in_tx=True,
                    )
                    extension_preview = extension_previews[op["id"]]
                    entity_type, target_uid, result_uid = op["type"], None, op["type"]
                    item["before"] = json.dumps({
                        "added": extension["added"],
                        "version_before": extension_preview["version_before"],
                        "version_after": extension["version"],
                    })
                    applied_properties = extension["type"]["json_schema"]["properties"]
                    item["after_hash"] = _hash_data({
                        field: applied_properties[field] for field in extension["added"]
                    })
                elif kind == "create_entity":
                    result = entities.create_entity(
                        hub, conn, op["type"], item["data"], actor,
                        uid=uid_by_op[op["id"]], precomputed=item["entity"],
                        in_tx=True, audit=False,
                    )
                    entity_type, target_uid, result_uid = op["type"], None, result["id"]
                elif kind == "update_entity":
                    row, current = _entity_data(conn, op["target"])
                    if row["status"] != "current" or _hash_data(current) != item["before_hash"]:
                        raise ApiError("capture_apply", "update target changed during apply", 409)
                    result = entities.patch_entity(
                        hub, conn, op["target"], item["final"], actor,
                        precomputed=item["entity"], in_tx=True, audit=False,
                    )
                    entity_type, target_uid, result_uid = row["type"], op["target"], result["id"]
                    item["final"] = result["data"]
                elif kind == "supersede_entity":
                    row, current = _entity_data(conn, op["target"])
                    if row["status"] != "current" or _hash_data(current) != item["before_hash"]:
                        raise ApiError("capture_apply", "supersede target changed during apply", 409)
                    result = entities.supersede_entity(
                        hub, conn, op["target"], item["final"], op["reason"], actor,
                        result_uid=uid_by_op[op["id"]], precomputed=item["entity"],
                        in_tx=True, audit=False,
                    )
                    entity_type, target_uid, result_uid = row["type"], op["target"], result["id"]
                else:
                    note_data = {
                        "title": item["title"], "content": item["content"],
                        "tags": item["tags"], "happened_at": item["happened_at"],
                    }
                    digest = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
                    duplicate = conn.execute(
                        "SELECT uid FROM notes WHERE content_hash=? AND status='current'",
                        (digest,),
                    ).fetchone()
                    if duplicate:
                        raise ApiError("duplicate", "an identical current note exists", 409)
                    note_id, result_uid = notes._insert(
                        hub, conn, title=item["title"], content=item["content"],
                        tags=item["tags"], happened_at=item["happened_at"], source="capture",
                        chunks=item["chunks"], vecs=item["vecs"],
                    )
                    del note_id, note_data
                    entity_type, target_uid = None, None
                    mirrored_notes.append(result_uid)
                after_hash = None
                before = item.get("before")
                if kind in ("update_entity", "supersede_entity"):
                    before = json.dumps(item["before"], ensure_ascii=False, sort_keys=True)
                if kind in ("update_entity", "supersede_entity"):
                    after_hash = _hash_data(item["final"])
                if kind == "extend_entity_type":
                    before = item["before"]
                    after_hash = item["after_hash"]
                conn.execute(
                    "INSERT INTO capture_ops(capture_id,op_id,kind,entity_type,target_uid,"
                    "result_uid,before,after_hash) VALUES(?,?,?,?,?,?,?,?)",
                    (capture_row["id"], op["id"], kind, entity_type, target_uid,
                     result_uid, before, after_hash),
                )
                applied.append({
                    "op_id": op["id"], "kind": kind, "id": result_uid,
                    "type": entity_type, "label": op["label"],
                })
            ids = [item["id"] for item in applied]
            conn.execute(
                "INSERT INTO audit_log(actor,action,target_kind,target_uid,detail) "
                "VALUES(?,?,?,?,?)",
                (actor, "capture_apply", "capture", uid, json.dumps(ids)),
            )
            changed = conn.execute(
                "UPDATE captures SET state='applied',plan=?,decided_at=? WHERE id=? "
                "AND state='ready'",
                (json.dumps(normalized, ensure_ascii=False), _now(), capture_row["id"]),
            ).rowcount
            if changed != 1:
                raise ApiError("capture_apply", "capture is no longer ready", 409)
    except Exception as exc:
        if isinstance(exc, ApiError) and exc.code == "capture_apply":
            raise ApiError("capture_apply", f"apply failed at {active_op}", 409,
                           op_id=active_op) from exc
        if isinstance(exc, ApiError) and exc.code == "duplicate":
            raise ApiError("capture_apply", f"apply failed at {active_op}", 409,
                           op_id=active_op) from exc
        raise ApiError("capture_apply", f"apply failed at {active_op}", 409,
                       op_id=active_op) from exc
    for note_uid in mirrored_notes:
        notes._mirror(hub, conn, note_uid)
    return {"applied": applied, "skipped": skipped}


def validate(hub, conn, uid: str, body: dict) -> dict:
    capture_row = jobs._row(conn, uid)
    if capture_row["state"] != "ready":
        raise ApiError("capture_state", "capture must be ready before validation", 409)
    if not isinstance(body, dict):
        raise ApiError("validation_error", "JSON object body required", 400)
    stored = json.loads(capture_row["plan"])
    raw_plan, overrides = _plan_input(stored, body)
    if "allow_new_types" in body and not isinstance(body["allow_new_types"], bool):
        raise ApiError("validation_error", "allow_new_types must be a boolean", 400)
    if set(body) - {"ops", "keep_note", "allow_new_types"}:
        raise ApiError("validation_error", "unsupported validation field", 400)
    raw_text = capture_row["raw_text"] or ""
    candidates = context.build_candidates(
        hub, conn, raw_text, hub.settings.capture_candidates
    ) if raw_text else []
    normalized = plan.normalize(
        conn, raw_plan, raw_text, candidates,
        max_ops=max(1, min(20, hub.settings.capture_max_ops)),
        settings=hub.settings,
    )
    by_id = {op["id"]: op for op in normalized["ops"]}
    for op_id, override in overrides.items():
        if "include" in override:
            by_id[op_id]["include"] = override["include"]

    selected = [op for op in normalized["ops"] if op.get("include", True)]
    selected_ids = {op["id"] for op in selected}
    missing = [
        {"op": op["id"], "requires": [required for required in op.get("requires", [])
                                      if required not in selected_ids]}
        for op in selected if any(required not in selected_ids for required in op.get("requires", []))
    ]
    disallowed_types = [
        op for op in selected
        if op["kind"] == "create_entity_type" and not body.get("allow_new_types", False)
    ]
    result_ops = []
    for op in normalized["ops"]:
        errors = list(op.get("errors", []))
        if op in disallowed_types:
            errors.append("allow_new_types is required")
        result_ops.append({
            "id": op["id"],
            "status": "invalid" if errors else op["status"],
            "errors": errors,
            "warnings": op.get("warnings", []),
            "diff": op.get("diff", []),
            "requires": op.get("requires", []),
            "label": op.get("label", ""),
            "risk": op.get("risk"),
        })
    invalid_selected = any(
        op.get("include", True) and op["status"] == "invalid" for op in normalized["ops"]
    )
    return {
        "ops": result_ops,
        "selection": {
            "ok": not missing and not invalid_selected and not disallowed_types,
            "missing": missing,
        },
    }


def undo(hub, conn, uid: str, actor: str) -> dict:
    capture_row = jobs._row(conn, uid)
    if capture_row["state"] != "applied":
        raise ApiError("capture_state", "only an applied capture can be undone", 409)
    ops = conn.execute(
        "SELECT * FROM capture_ops WHERE capture_id=? AND status='applied' ORDER BY id DESC",
        (capture_row["id"],),
    ).fetchall()
    prepared: dict[int, object] = {}

    for item in ops:
        if item["kind"] != "update_entity" or not item["target_uid"]:
            continue
        row = conn.execute(
            "SELECT * FROM entities WHERE uid=?", (item["target_uid"],)
        ).fetchone()
        if row is None or _hash_data(json.loads(row["data"])) != item["after_hash"]:
            continue
        before = json.loads(item["before"])
        type_info = entities.get_type(conn, row["type"])
        prepared[item["id"]] = entities._prepare(hub, type_info, before)

    undone, kept_modified, kept_in_use, types_left = [], [], [], []
    mirrored_notes: list[str] = []
    with tx(conn):
        for item in ops:
            kind, result_uid, target_uid = item["kind"], item["result_uid"], item["target_uid"]
            if kind == "create_entity":
                conn.execute(
                    "UPDATE entities SET status='archived',status_reason='capture undone',"
                    "updated_at=? WHERE uid=?",
                    (_now(), result_uid),
                )
            elif kind == "create_note":
                conn.execute(
                    "UPDATE notes SET status='archived',status_reason='capture undone',"
                    "updated_at=? WHERE uid=?",
                    (_now(), result_uid),
                )
                mirrored_notes.append(result_uid)
            elif kind == "create_entity_type":
                types_left.append(item["entity_type"])
            elif kind == "extend_entity_type":
                before = json.loads(item["before"])
                removed = entity_types.undo_extension(
                    conn, item["entity_type"], before["added"], before["version_after"],
                    uid, actor, in_tx=True,
                )
                if not removed:
                    kept_in_use.append(item["op_id"])
            elif kind == "supersede_entity":
                conn.execute(
                    "UPDATE entities SET status='archived',status_reason='capture undone',"
                    "updated_at=? WHERE uid=?",
                    (_now(), result_uid),
                )
                conn.execute(
                    "UPDATE entities SET status='current',superseded_by=NULL,status_reason=NULL,"
                    "updated_at=? WHERE uid=?",
                    (_now(), target_uid),
                )
            elif kind == "update_entity":
                current = conn.execute(
                    "SELECT * FROM entities WHERE uid=?", (target_uid,)
                ).fetchone()
                if (current is None or
                        _hash_data(json.loads(current["data"])) != item["after_hash"]):
                    kept_modified.append(item["op_id"])
                    conn.execute("UPDATE capture_ops SET status='undone' WHERE id=?", (item["id"],))
                    continue
                before = json.loads(item["before"])
                type_row = conn.execute(
                    "SELECT * FROM entity_types WHERE name=?", (current["type"],)
                ).fetchone()
                try:
                    entities._check_unique(conn, type_row, before, current["id"])
                except ApiError:
                    kept_modified.append(item["op_id"])
                    conn.execute("UPDATE capture_ops SET status='undone' WHERE id=?", (item["id"],))
                    continue
                conn.execute(
                    "UPDATE entities SET data=?,updated_at=? WHERE id=?",
                    (json.dumps(before, ensure_ascii=False), _now(), current["id"]),
                )
                entities._write_chunk(hub, conn, current["id"], prepared[item["id"]])
            undone.append(item["op_id"])
            conn.execute("UPDATE capture_ops SET status='undone' WHERE id=?", (item["id"],))
        changed = conn.execute(
            "UPDATE captures SET state='undone',decided_at=? WHERE id=? AND state='applied'",
            (_now(), capture_row["id"]),
        ).rowcount
        if changed != 1:
            raise ApiError("capture_state", "capture is no longer applied", 409)
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid,detail) VALUES(?,?,?,?,?)",
            (actor, "capture_undo", "capture", uid,
             json.dumps({"undone": undone, "kept_modified": kept_modified,
                         "kept_in_use": kept_in_use})),
        )
        self_svc.reconcile(conn)
    for note_uid in mirrored_notes:
        notes._mirror(hub, conn, note_uid)
    return {"undone": undone, "kept_modified": kept_modified,
            "kept_in_use": kept_in_use, "types_left": types_left}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
