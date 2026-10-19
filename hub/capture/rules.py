"""Deterministic type-mapping rules applied to model operations before validation."""
from __future__ import annotations

import re

ACTION_VERBS = {
    "call", "book", "send", "renew", "review", "simplify", "read", "buy", "pay", "schedule",
    "finish", "submit", "write", "email", "fix", "order", "check", "confirm", "drop",
    "pick", "start", "update", "prepare", "clean", "cancel", "appeler", "reserver",
    "envoyer", "renouveler", "lire", "acheter", "payer",
}
CADENCE_RE = re.compile(
    r"\b(daily|weekly|every|each|chaque|quotidien|quotidienne|hebdomadaire|"
    r"tous les|toutes les)\b",
    re.IGNORECASE,
)
FINANCIAL_RE = re.compile(
    r"\b(insurance|premium|policy|visa|mastercard|credit|debit|card|account|salary|"
    r"mortgage|loan|iban|assurance|prime|hypoth[eè]que|dollars?|cad|usd|eur)\b|[$€£]",
    re.IGNORECASE,
)
DEADLINE_FIELDS = ("due", "due_date", "deadline")
DUE_FIELDS = ("due_date", "due", "deadline", "date")


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _title(data: dict) -> str:
    for key in ("title", "name"):
        if isinstance(data.get(key), str):
            return data[key]
    return ""


def has_action(data: dict) -> bool:
    first = re.findall(r"[\w']+", _title(data).casefold())[:1]
    return bool(first and first[0] in ACTION_VERBS) or any(k in data for k in DEADLINE_FIELDS)


def has_cadence(op: dict) -> bool:
    text = " ".join([*_strings(op.get("data")), str(op.get("why") or "")])
    return bool(CADENCE_RE.search(text))


def map_type(op: dict, types: dict[str, dict]) -> tuple[dict, tuple[str, str] | None]:
    """Return (op, reject) where reject is (reason code, human message) or None."""
    if op.get("op") != "create_entity" or not isinstance(op.get("data"), dict):
        return op, None
    type_name = op.get("type")
    data = op["data"]
    if type_name == "habit" and not has_cadence(op):
        return op, ("not_a_habit", "A habit needs a cadence (daily, weekly, every, each).")
    if type_name == "event" and "task" in types and has_action(data):
        props = types["task"]["json_schema"].get("properties", {})
        mapped = {k: v for k, v in data.items() if k not in ("date", "kind", "people")}
        if "date" in data:
            due = next((f for f in DUE_FIELDS if f in props), None)
            if due:
                mapped[due] = data["date"]
        if "state" in props:
            mapped["state"] = "pending"
        return {**op, "type": "task", "data": mapped}, None
    if type_name == "task" and "task" in types:
        props = types["task"]["json_schema"].get("properties", {})
        if "state" in props and not data.get("state"):
            return {**op, "data": {**data, "state": "pending"}}, None
    return op, None


def has_financial(*values: object) -> bool:
    return any(FINANCIAL_RE.search(text) for value in values for text in _strings(value))
