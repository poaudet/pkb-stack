"""Selectable LLM endpoints: a JSON array in LLM_ENDPOINTS, choice stored per purpose in meta."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from sqlite3 import Connection

from . import db
from .config import Settings

log = logging.getLogger(__name__)

PURPOSES = ("tidy", "capture")


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str
    model: str
    api_key: str = ""


def parse(raw: str) -> list[Endpoint]:
    """Parse LLM_ENDPOINTS: [{"name","base_url","model","api_key"?}]. Invalid entries are skipped."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("LLM_ENDPOINTS is not valid JSON; ignoring")
        return []
    if not isinstance(data, list):
        log.warning("LLM_ENDPOINTS must be a JSON array; ignoring")
        return []
    out: list[Endpoint] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        base_url, model = item.get("base_url"), item.get("model")
        if not isinstance(base_url, str) or not isinstance(model, str) or not model.strip():
            continue
        name = item.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else model.strip()
        key = item.get("api_key")
        if name in seen:
            continue
        seen.add(name)
        out.append(Endpoint(name, base_url.strip(), model.strip(),
                            key if isinstance(key, str) else ""))
    return out


def _meta_key(purpose: str) -> str:
    if purpose not in PURPOSES:
        raise ValueError("unknown purpose")
    return f"llm_endpoint_{purpose}"


def selected(conn: Connection, settings: Settings, purpose: str) -> Endpoint | None:
    """The chosen endpoint; defaults to the first one. None when none are configured."""
    eps = parse(settings.llm_endpoints)
    if not eps:
        return None
    name = db.get_meta(conn, _meta_key(purpose))
    return next((e for e in eps if e.name == name), eps[0])


def select(conn: Connection, settings: Settings, purpose: str, name: str) -> None:
    if not any(e.name == name for e in parse(settings.llm_endpoints)):
        raise KeyError(name)
    db.set_meta(conn, _meta_key(purpose), name)


def effective(conn: Connection, settings: Settings, purpose: str) -> Settings:
    """Settings with the selected endpoint applied; unchanged when no endpoints are configured."""
    ep = selected(conn, settings, purpose)
    if ep is None:
        return settings
    if purpose == "tidy":
        return replace(settings, llm_base_url=ep.base_url, llm_model=ep.model,
                       llm_api_key=ep.api_key or "not-needed")
    return replace(settings, capture_llm_base_url=ep.base_url, capture_llm_model=ep.model,
                   capture_llm_api_key=ep.api_key)
