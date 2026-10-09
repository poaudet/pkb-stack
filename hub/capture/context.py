"""Bounded, privacy-safe type catalog and entity candidate construction."""
from __future__ import annotations

import json
import re

from .. import search
from ..db import fold
from ..errors import ApiError


def _field_type(prop: dict) -> str:
    if "enum" in prop:
        return "|".join(str(x) for x in prop["enum"])
    if prop.get("x-ref"):
        return "->" + str(prop["x-ref"])
    if prop.get("format") == "date":
        return "date"
    if prop.get("format") == "date-time":
        return "datetime"
    kind = prop.get("type", "text")
    if kind == "array":
        items = prop.get("items") or {}
        return "[text]" if items.get("type") == "string" else f"[{items.get('type', 'text')}]"
    return {"string": "text", "integer": "int"}.get(kind, kind)


def build_catalog(conn, exclude_types: str = "place_visit") -> str:
    excluded = {part.strip() for part in exclude_types.split(",") if part.strip()}
    lines: list[str] = []
    for row in conn.execute("SELECT * FROM entity_types ORDER BY name"):
        if row["name"] in excluded:
            continue
        schema = json.loads(row["json_schema"])
        required = set(schema.get("required", []))
        props = schema.get("properties", {})
        unique_key = row["unique_key"]
        fields = []
        for name, prop in list(props.items())[:40]:
            suffix = ("!" if name in required else "") + ("*" if name == unique_key else "")
            fields.append(f"{name}{suffix}:{_field_type(prop)}")
        desc = row["description"][:80].replace("\n", " ")
        lines.append(f"{row['name']} — {desc}: {' '.join(fields)}")
    out = "\n".join(lines)
    return out[:3500]


def _candidate_row(conn, uid: str) -> dict | None:
    row = conn.execute(
        "SELECT uid,type,data FROM entities WHERE uid=? AND status='current'", (uid,)
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row["data"])
    type_row = conn.execute(
        "SELECT unique_key FROM entity_types WHERE name=?", (row["type"],)
    ).fetchone()
    keys = []
    for key in dict.fromkeys((type_row["unique_key"], "name", "title")):
        value = data.get(key) if key else None
        if isinstance(value, (str, int, float)) and str(value).strip():
            keys.append(f"{key}={str(value).replace(chr(10), ' ')[:80]}")
    label = row["type"]
    for key in dict.fromkeys((type_row["unique_key"], "name", "title")):
        value = data.get(key) if key else None
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            label = str(value)
            break
    return {
        "id": row["uid"],
        "type": row["type"],
        "label": label[:80],
        "key_fields": "; ".join(keys)[:160],
    }


def build_candidates(hub, conn, text: str, limit: int = 25) -> list[dict]:
    folded_text = fold(text) or ""
    deterministic: list[tuple[int, dict]] = []
    for row in conn.execute(
        "SELECT e.uid,e.type,e.data,t.unique_key FROM entities e "
        "JOIN entity_types t ON t.name=e.type WHERE e.status='current' "
        "ORDER BY e.id DESC"
    ):
        data = json.loads(row["data"])
        label = None
        for key in dict.fromkeys((row["unique_key"], "name", "title")):
            value = data.get(key) if key else None
            if (isinstance(value, (str, int, float)) and not isinstance(value, bool)
                    and str(value).strip()):
                label = str(value).strip()
                break
        folded_label = fold(label) if label else ""
        if folded_label and re.search(
            r"(?<!\w)" + re.escape(folded_label) + r"(?!\w)", folded_text, re.UNICODE
        ):
            candidate = _candidate_row(conn, row["uid"])
            if candidate:
                deterministic.append((folded_text.find(folded_label), candidate))
    deterministic.sort(key=lambda item: item[0])
    selected = [item[1] for item in deterministic[:15]]
    seen = {item["id"] for item in selected}
    remaining = max(0, limit - len(selected))
    if remaining:
        try:
            hits = search.search(hub, conn, text[:1000], limit=min(10, remaining),
                                 kinds=["entity"])["hits"]
        except (ApiError, ValueError, RuntimeError):  # semantic search is optional context
            hits = []
        for hit in hits:
            uid = hit.get("id")
            if uid in seen:
                continue
            candidate = _candidate_row(conn, uid)
            if candidate:
                selected.append(candidate)
                seen.add(uid)
    return selected[:max(0, limit)]


def candidate_lines(candidates: list[dict]) -> str:
    if not candidates:
        return "(none)"
    return "\n".join(
        f"{c['id']} | {c['type']} | {c['label']} | {c['key_fields']}"
        for c in candidates
    )
