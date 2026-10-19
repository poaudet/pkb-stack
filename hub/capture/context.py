"""Bounded, privacy-safe type catalog and entity candidate construction."""
from __future__ import annotations

import json
import re

from .. import search
from .. import self as self_svc
from ..db import fold
from ..errors import ApiError
from ..policy import capture_excluded_types


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


def build_catalog(
    conn, exclude_types: str = "", max_chars: int = 5000
) -> str:
    excluded = capture_excluded_types(conn, exclude_types)
    entries: list[dict] = []
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
            description = str(prop.get("description") or "").replace("\n", " ")[:60]
            fields.append({"name": name, "suffix": suffix, "type": _field_type(prop),
                           "description": description, "protected": bool(prop.get("x-origin"))})
        desc = row["description"][:80].replace("\n", " ")
        entries.append({"name": row["name"], "description": desc, "fields": fields,
                        "builtin": bool(row["builtin"])})

    def render() -> str:
        lines = []
        for entry in entries:
            fields = [
                f"{field['name']}{field['suffix']}:{field['type']}"
                + (f" ({field['description']})" if field["description"] else "")
                for field in entry["fields"]
            ]
            lines.append(f"{entry['name']} — {entry['description']}: {' '.join(fields)}")
        return "\n".join(lines)

    budget = max(0, max_chars)
    out = render()
    description_fields = [
        field for entry in entries if entry["builtin"]
        for field in entry["fields"] if field["description"] and not field["protected"]
    ] + [
        field for entry in entries if not entry["builtin"]
        for field in entry["fields"] if field["description"] and not field["protected"]
    ]
    for field in description_fields:
        if len(out) <= budget:
            break
        field["description"] = ""
        out = render()
    return out[:budget]


def _candidate_row(conn, uid: str, excluded: set[str]) -> dict | None:
    row = conn.execute(
        "SELECT uid,type,data FROM entities WHERE uid=? AND status='current'", (uid,)
    ).fetchone()
    if row is None or row["type"] in excluded:
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


def build_candidates(
    hub, conn, text: str, limit: int = 25, exclude_types: str = ""
) -> list[dict]:
    excluded = capture_excluded_types(conn, exclude_types)
    folded_text = fold(text) or ""
    deterministic: list[tuple[int, dict]] = []
    for row in conn.execute(
        "SELECT e.uid,e.type,e.data,t.unique_key FROM entities e "
        "JOIN entity_types t ON t.name=e.type WHERE e.status='current' "
        "AND t.capture_visible=1 "
        "ORDER BY e.id DESC"
    ):
        if row["type"] in excluded:
            continue
        data = json.loads(row["data"])
        if (row["type"] == "person" and isinstance(data.get("relationship"), str)
                and fold(data["relationship"]) == "self"):
            continue
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
            candidate = _candidate_row(conn, row["uid"], excluded)
            if candidate:
                deterministic.append((folded_text.find(folded_label), candidate))
    deterministic.sort(key=lambda item: item[0])
    selected = [item[1] for item in deterministic[:15]]
    seen = {item["id"] for item in selected}
    remaining = max(0, limit - len(selected))
    if remaining:
        try:
            visible_types = [
                row["name"] for row in conn.execute(
                    "SELECT name FROM entity_types WHERE capture_visible=1"
                ) if row["name"] not in excluded
            ]
            hits = search.search(
                hub, conn, text[:1000], limit=min(10, remaining), kinds=["entity"],
                types=visible_types,
            )["hits"] if visible_types else []
        except (ApiError, ValueError, RuntimeError):  # semantic search is optional context
            hits = []
        for hit in hits:
            uid = hit.get("id")
            if uid in seen:
                continue
            candidate = _candidate_row(conn, uid, excluded)
            if candidate:
                selected.append(candidate)
                seen.add(uid)
    return selected[:max(0, limit)]


def self_line(conn, exclude_types: str = "") -> str:
    owner = self_svc.get_self(conn)["self"]
    if owner is None or owner["type"] in capture_excluded_types(conn, exclude_types):
        return "SELF: none yet"
    data = owner["data"]
    details = []
    for field in ("interests", "habits"):
        value = data.get(field)
        if value:
            details.append(f"{field}: {_line_value(value)}")
    summary = "; ".join(details)[:160]
    name = str(data.get("name", "")).replace("\n", " ")[:80]
    return f"SELF | {owner['id']} | person | {name} | {summary}"


def _line_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def is_first_person(text: str) -> bool:
    folded = fold(text) or ""
    return bool(re.search(
        r"\b(i|i'm|i've|i'd|i'll|me|my|mine|myself|je|moi|mon|ma|mes)\b",
        folded,
    ) or re.search(r"\b(j'|m')", folded))


def candidate_lines(candidates: list[dict], owner_line: str = "SELF: none yet") -> str:
    lines = [owner_line]
    lines.extend(
        f"{c['id']} | {c['type']} | {c['label']} | {c['key_fields']}"
        for c in candidates
    )
    return "\n".join(lines)
