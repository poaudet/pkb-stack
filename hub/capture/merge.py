"""Merge per-chunk plans into one reviewable plan for a long capture job."""
from __future__ import annotations

import copy
import re

from . import names, plan

RAW_KEYS = (
    "id", "op", "type", "name", "ref", "target", "data", "patch", "reason", "description",
    "json_schema", "text_template", "unique_key", "embed", "add_properties", "why",
)


def _label(op: dict) -> str:
    data = op.get("data")
    if isinstance(data, dict):
        for key in ("name", "title"):
            if isinstance(data.get(key), str):
                return names.norm_name(data[key])
    return ""


def _rewrite(value: object, ref_map: dict[str, str]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$ref"} and value["$ref"] in ref_map:
            return {"$ref": ref_map[value["$ref"]]}
        return {key: _rewrite(item, ref_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, ref_map) for item in value]
    return value


def _merge_data(first: dict, other: dict, type_name: str, notes: list[str]) -> None:
    for key, value in other.items():
        if key not in first:
            first[key] = copy.deepcopy(value)
        elif first[key] == value:
            continue
        elif isinstance(first[key], list) and isinstance(value, list):
            first[key].extend(item for item in value if item not in first[key])
        else:
            notes.append(f"conflict:{type_name}.{key}")


def merge(chunk_plans: list[tuple[int, dict]], max_ops: int
          ) -> tuple[list[dict], dict[str, list[int]], dict[str, list[str]], list[str]]:
    """Return (raw ops, provenance by op id, warnings by op id, plan warnings)."""
    ops: list[dict] = []
    provenance: dict[str, list[int]] = {}
    op_warnings: dict[str, list[str]] = {}
    plan_warnings: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    ref_map: dict[str, str] = {}
    for idx, chunk_plan in chunk_plans:
        for op in chunk_plan.get("ops", []):
            if op.get("kind") == "create_note":
                continue
            raw = {key: copy.deepcopy(op[key]) for key in RAW_KEYS if key in op}
            key = (str(raw.get("type")), _label(raw))
            if raw.get("op") == "create_entity" and key[1] and key in seen:
                first = ops[seen[key]]
                notes = op_warnings.setdefault(first["id"], [])
                notes.append(f"merged_duplicate:{key[0]}")
                _merge_data(first["data"], raw.get("data", {}), key[0], notes)
                if raw.get("ref"):
                    if first.get("ref"):
                        ref_map[raw["ref"]] = first["ref"]
                    else:
                        first["ref"] = raw["ref"]
                provenance[first["id"]] = sorted({*provenance[first["id"]], idx})
                continue
            if raw.get("op") == "create_entity" and key[1]:
                seen[key] = len(ops)
            provenance[raw["id"]] = [idx]
            ops.append(raw)
    for raw in ops:
        for field in ("data", "patch"):
            if field in raw:
                raw[field] = _rewrite(raw[field], ref_map)
    while True:
        defined = {raw["ref"] for raw in ops if raw.get("ref")}
        broken = [raw for raw in ops
                  if any(ref not in defined for ref in plan._walk_refs(
                      raw.get("data", raw.get("patch", {}))))]
        if not broken:
            break
        for raw in broken:
            ops.remove(raw)
            provenance.pop(raw["id"], None)
            op_warnings.pop(raw["id"], None)
            plan_warnings.append("dropped_unresolved_reference")
    if len(ops) > max_ops:
        for raw in ops[max_ops:]:
            provenance.pop(raw["id"], None)
            op_warnings.pop(raw["id"], None)
        ops = ops[:max_ops]
        plan_warnings.append("plan_truncated")
    return ops, provenance, op_warnings, plan_warnings


_PREFIXED = re.compile(r"^c\d+_")


def prefix_plan(normalized: dict, idx: int) -> dict:
    """Make op ids and refs unique per job by prefixing them with the chunk index."""
    prefix = f"c{idx}_"
    id_map: dict[str, str] = {}
    ref_map: dict[str, str] = {}
    for op in normalized["ops"]:
        if not _PREFIXED.match(op["id"]):
            id_map[op["id"]] = (prefix + op["id"])[:40]
        if op.get("ref") and not _PREFIXED.match(op["ref"]):
            ref_map[op["ref"]] = prefix + op["ref"]
    for op in normalized["ops"]:
        op["id"] = id_map.get(op["id"], op["id"])
        if op.get("ref"):
            op["ref"] = ref_map.get(op["ref"], op["ref"])
        op["requires"] = [id_map.get(item, item) for item in op.get("requires", [])]
        if isinstance(op.get("gap"), dict) and "needed_by" in op["gap"]:
            op["gap"]["needed_by"] = [id_map.get(i, i) for i in op["gap"]["needed_by"]]
        for field in ("data", "patch"):
            if field in op:
                op[field] = _rewrite(op[field], ref_map)
    return normalized
