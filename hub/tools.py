"""Agent tools: single source of truth for the manifest and the dispatcher."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from . import entities, entity_types, notes, pending, policy
from . import search as search_svc
from . import self as self_svc
from .core import Hub
from .errors import ApiError, validation

Handler = Callable[[Hub, object, dict, str], object]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    writes: bool = False
    destructive: bool = False


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [],
            "additionalProperties": False}


_STR = {"type": "string"}
_FILTERS = {
    "type": "array", "maxItems": 20,
    "description": "List of {field, op, value}. ops: eq, ieq (case-insensitive equal), ne, gt, "
                   "gte, lt, lte (ISO dates compare correctly), contains (case-insensitive "
                   "substring), in (value is a list), exists (value true/false), has (array "
                   "field contains value).",
    "items": _obj({"field": _STR, "op": {"type": "string", "enum": list(entities.OPS)},
                   "value": {}}, ["field", "op"]),
}


# ---- handlers: (hub, conn, args, actor) -> JSON-serialisable result

def _search(hub, conn, a, actor):
    visible_types = [
        row["name"] for row in conn.execute(
            "SELECT name FROM entity_types WHERE agent_visible=1"
        )
    ]
    if a.get("types") is not None:
        visible_types = [name for name in visible_types if name in a["types"]]
    return search_svc.search(
        hub, conn, a["q"], limit=a.get("limit", 10), kinds=a.get("kinds"), tags=a.get("tags"),
        include_superseded=a.get("include_superseded", False),
        include_archived=a.get("include_archived", False), since=a.get("since"),
        until=a.get("until"), types=a.get("types"), visible_types=visible_types)


def _get_self(conn):
    owner = self_svc.get_self(conn)["self"]
    if owner and not policy.is_agent_visible(conn, owner["type"]):
        return {"exists": False}
    return {"exists": False} if owner is None else {"entity": owner}


def _create_note(hub, conn, a, actor):
    data = {k: a[k] for k in ("content", "title", "tags", "happened_at") if k in a}
    if actor.startswith("agent:") and hub.settings.agent_notes_require_review:
        return {"pending": pending.create(hub, conn, data, actor, queue=False),
                "note": "queued for human review"}
    return {"note": notes.create_note(hub, conn, data, actor)}


def _supersede_note(hub, conn, a, actor):
    data = {k: a[k] for k in ("content", "title", "reason", "tags") if k in a}
    return {"note": notes.supersede_note(hub, conn, a["id"], data, actor)}


def _query(hub, conn, a, actor):
    policy.require_agent_visible(conn, a["type"])
    return entities.query(conn, a)


def _forget_note(hub, conn, a, actor):
    notes.forget_note(hub, conn, a["id"], actor, a.get("reason"))
    return {"forgotten": a["id"]}


def _forget_entity(hub, conn, a, actor):
    policy.require_entity_agent_visible(conn, a["id"])
    entities.forget_entity(hub, conn, a["id"], actor, a.get("reason"))
    return {"forgotten": a["id"]}


def _create_entity(hub, conn, args, actor):
    policy.require_agent_visible(conn, args["type"])
    return {"entity": entities.create_entity(hub, conn, args["type"], args["data"], actor)}


def _get_entity(conn, uid):
    policy.require_entity_agent_visible(conn, uid)
    return {"entity": entities.get_entity(conn, uid)}


def _patch_entity(hub, conn, args, actor):
    policy.require_entity_agent_visible(conn, args["id"])
    return {"entity": entities.patch_entity(hub, conn, args["id"], args["patch"], actor)}


def _supersede_entity(hub, conn, args, actor):
    policy.require_entity_agent_visible(conn, args["id"])
    return {"entity": entities.supersede_entity(
        hub, conn, args["id"], args["data"], args.get("reason"), actor)}


def _archive_entity(hub, conn, args, actor):
    policy.require_entity_agent_visible(conn, args["id"])
    return {"entity": entities.set_status(
        conn, args["id"], "archived", args.get("reason"), actor)}


def _extend_entity_type(hub, conn, args, actor):
    if not hub.settings.tools_allow_extend:
        from .errors import ApiError

        raise ApiError("forbidden", "type extension tool is disabled", 403)
    name = args["name"]
    policy.require_agent_visible(conn, name)
    type_info = entities.get_type(conn, name)
    properties = args["add_properties"]
    if not isinstance(properties, dict) or not 1 <= len(properties) <= 5:
        raise validation("add_properties must contain 1-5 properties")
    current = type_info["json_schema"].get("properties", {})
    additions = {}
    for field, definition in properties.items():
        existing = current.get(field)
        if existing is None:
            additions[field] = definition
            continue
        comparable = {key: value for key, value in existing.items()
                      if key not in ("x-origin", "x-added")}
        if comparable != definition:
            from .errors import ApiError

            raise ApiError("conflict", f"property {field!r} already exists differently", 409)
    if not additions:
        return {"applied": False, "reason": "exists", "schema_version": type_info["schema_version"]}
    preview = entity_types.preview(conn, name, additions, max_new=5)
    if not args.get("confirm", False):
        return {
            "applied": False,
            "diff": [{"name": field, "definition": definition}
                     for field, definition in preview["added"].items()],
            "schema_version": type_info["schema_version"],
        }
    extension_actor = actor if actor.startswith("agent:") else f"agent:{actor[:74]}"
    result = entity_types.extend(conn, name, additions, actor=extension_actor, max_new=5)
    return {"applied": True, "schema_version": result["version"], "type": result["type"]}


def _type_args(a: dict) -> dict:
    return {k: a[k] for k in ("name", "description", "json_schema", "text_template",
                              "unique_key", "embed", "policy") if k in a}


_ID = {"type": "string", "description": "The id returned by search or a previous call."}

TOOLS: list[Tool] = [
    Tool("get_self",
         "Use to read the owner's own facts (preferences, habits, birthday) before asking them.",
         _obj({}), lambda h, c, a, u: _get_self(c)),
    Tool("search_knowledge",
         "Semantic + keyword search over the owner's notes AND entities (people, events, "
         "messages, memories, custom types). Use it for open questions like 'what do I know "
         "about X'. Use query_entities instead when you need exact fields (dates, names). "
         "Superseded/archived items are hidden unless requested. "
         "Example: {\"q\": \"RAV4 collision sensor\", \"limit\": 5}.",
         _obj({"q": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 50},
               "kinds": {"type": "array", "items": {"type": "string", "enum": ["note", "entity"]}},
               "types": {"type": "array", "items": _STR,
                         "description": "Restrict to these entity types."},
               "tags": {"type": "array", "items": _STR},
               "include_superseded": {"type": "boolean"},
               "include_archived": {"type": "boolean"},
               "since": {"type": "string", "description": "YYYY-MM-DD"},
               "until": {"type": "string", "description": "YYYY-MM-DD"}}, ["q"]),
         _search),
    Tool("get_note", "Fetch one note by id (full content). Example: {\"id\": \"3f9a...\"}.",
         _obj({"id": _ID}, ["id"]), lambda h, c, a, u: {"note": notes.get_note(c, a["id"])}),
    Tool("create_note",
         "Save a free-text note (markdown). Use for information that does not fit a structured "
         "entity. Agent notes go to a human review queue first. "
         "Example: {\"content\": \"Dentist moved to May 3rd\", \"tags\": [\"health\"]}.",
         _obj({"content": _STR, "title": _STR, "tags": {"type": "array", "items": _STR},
               "happened_at": {"type": "string", "description": "YYYY-MM-DD the note is about"}},
              ["content"]), _create_note, writes=True),
    Tool("supersede_note",
         "Replace a note whose information is no longer true with a new version; the old one is "
         "kept as history but hidden from search. Example: {\"id\": \"3f9a...\", "
         "\"content\": \"Dentist is on May 3rd\", \"reason\": \"rescheduled\"}.",
         _obj({"id": _ID, "content": _STR, "title": _STR, "reason": _STR,
               "tags": {"type": "array", "items": _STR}}, ["id", "content"]),
         _supersede_note, writes=True),
    Tool("archive_note",
         "Hide a note from search without replacing it (undoable). "
         "Example: {\"id\": \"3f9a...\", \"reason\": \"obsolete\"}.",
         _obj({"id": _ID, "reason": _STR}, ["id"]),
         lambda h, c, a, u: {"note": notes.set_status(h, c, a["id"], "archived", a.get("reason"), u)},
         writes=True),
    Tool("list_entity_types",
         "List entity types with their JSON Schema (fields) and unique key. Call this first to "
         "learn which fields can be used in create_entity and query_entities. Example: {}.",
         _obj({}), lambda h, c, a, u: {
             "types": entities.list_types(c, agent_visible_only=True)}),
    Tool("create_entity_type",
         "Define a NEW kind of record (e.g. 'recipe', 'car'). Only when no existing type fits. "
         "json_schema: top-level type object, 1-50 properties of type string (format date or "
         "date-time), number, integer, boolean, array of scalars, or a flat object. "
         "text_template uses {field} placeholders. unique_key is a field that must be unique "
         "among current records. Example: {\"name\": \"recipe\", \"json_schema\": {\"type\": "
         "\"object\", \"required\": [\"title\"], \"properties\": {\"title\": {\"type\": "
         "\"string\"}, \"ingredients\": {\"type\": \"array\", \"items\": {\"type\": \"string\"}}}},"
         " \"text_template\": \"{title}: {ingredients}\", \"unique_key\": \"title\"}.",
         _obj({"name": _STR, "description": _STR, "json_schema": {"type": "object"},
               "text_template": _STR, "unique_key": _STR, "embed": {"type": "boolean"},
               "policy": {"type": "object", "properties": {
                   "allow_extension": {"type": "boolean"},
               }, "additionalProperties": True}},
              ["name", "json_schema"]),
         lambda h, c, a, u: {"type": entities.create_type(c, _type_args(a), u)}, writes=True),
    Tool("create_entity",
         "Store a structured record of an existing type (see list_entity_types). Fails with "
         "'conflict' if the unique key already exists: then use update_entity or "
         "supersede_entity. Example: {\"type\": \"person\", \"data\": {\"name\": \"Alice\", "
         "\"birthday\": \"1990-04-12\", \"relationship\": \"friend\"}}.",
         _obj({"type": _STR, "data": {"type": "object"}}, ["type", "data"]),
         _create_entity,
         writes=True),
    Tool("extend_entity_type",
         "Propose an additive type extension with confirm=false first. Types with "
         "policy.allow_extension=false cannot be extended. Show the returned diff "
         "to the owner, then call again with confirm=true only after explicit approval. "
         "Use a note instead when approval is not given.",
         _obj({"name": _STR, "add_properties": {"type": "object"},
              "confirm": {"type": "boolean"}},
             ["name", "add_properties"]),
         _extend_entity_type, writes=True),
    Tool("query_entities",
         "Exact structured lookup of entities. Use for 'when', 'who', 'last time' questions; "
         "reason over the returned rows yourself. Fields must be top-level fields of the type. "
         "Examples: last time I went to X: {\"type\": \"event\", \"filters\": [{\"field\": "
         "\"location\", \"op\": \"contains\", \"value\": \"X\"}], \"order_by\": {\"field\": "
         "\"date\", \"dir\": \"desc\"}, \"limit\": 1}. Alice's birthday: {\"type\": \"person\", "
         "\"filters\": [{\"field\": \"name\", \"op\": \"ieq\", \"value\": \"Alice\"}]}. Last "
         "exchange with my vet: {\"type\": \"message\", \"filters\": [{\"field\": \"contact\", "
         "\"op\": \"ieq\", \"value\": \"vet\"}], \"order_by\": {\"field\": \"at\", \"dir\": "
         "\"desc\"}, \"limit\": 20}. Friends' habits: {\"type\": \"person\", \"filters\": "
         "[{\"field\": \"relationship\", \"op\": \"ieq\", \"value\": \"friend\"}]}.",
         _obj({"type": _STR, "filters": _FILTERS,
               "order_by": _obj({"field": _STR, "dir": {"type": "string", "enum": ["asc", "desc"]}},
                                ["field"]),
               "limit": {"type": "integer", "minimum": 1, "maximum": 200},
               "offset": {"type": "integer", "minimum": 0},
               "include_superseded": {"type": "boolean"}}, ["type"]),
         _query),
    Tool("get_entity", "Fetch one entity by id. Example: {\"id\": \"3f9a...\"}.",
         _obj({"id": _ID}, ["id"]),
         lambda h, c, a, u: _get_entity(c, a["id"])),
    Tool("update_entity",
         "Change some fields of an entity in place (JSON Merge Patch: set a field to null to "
         "remove it; arrays are replaced whole). Use for corrections. Example: {\"id\": "
         "\"3f9a...\", \"patch\": {\"relationship\": \"colleague\"}}.",
         _obj({"id": _ID, "patch": {"type": "object"}}, ["id", "patch"]),
         _patch_entity,
         writes=True),
    Tool("supersede_entity",
         "Replace an entity whose facts changed with a complete new document; the old one is "
         "kept as history. Example: {\"id\": \"3f9a...\", \"data\": {\"name\": \"Alice\", "
         "\"relationship\": \"ex-friend\"}, \"reason\": \"fell out\"}.",
         _obj({"id": _ID, "data": {"type": "object"}, "reason": _STR}, ["id", "data"]),
         _supersede_entity, writes=True),
    Tool("archive_entity",
         "Hide an entity from queries and search (undoable). Example: {\"id\": \"3f9a...\"}.",
         _obj({"id": _ID, "reason": _STR}, ["id"]),
         _archive_entity, writes=True),
    Tool("forget_note", "PERMANENTLY delete a note and every trace of it. Cannot be undone. "
         "Example: {\"id\": \"3f9a...\"}.", _obj({"id": _ID, "reason": _STR}, ["id"]),
         _forget_note, writes=True, destructive=True),
    Tool("forget_entity", "PERMANENTLY delete an entity and every trace of it. Cannot be undone. "
         "Example: {\"id\": \"3f9a...\"}.", _obj({"id": _ID, "reason": _STR}, ["id"]),
         _forget_entity, writes=True, destructive=True),
]


def available(hub: Hub) -> dict[str, Tool]:
    return {t.name: t for t in TOOLS if hub.settings.tools_allow_destructive or not t.destructive}


def manifest(hub: Hub) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in available(hub).values()]


def call(hub: Hub, conn, name: str, args: object, actor: str) -> dict:
    """Run a tool. Tool-level errors come back as ok:false (HTTP 200 at the API layer)."""
    tool = available(hub).get(name)
    if tool is None:
        raise ApiError("not_found", f"unknown tool {name!r}", 404)
    try:
        if not isinstance(args, dict):
            raise validation("arguments must be a JSON object")
        errs = list(Draft202012Validator(tool.parameters).iter_errors(args))
        if errs:
            e = errs[0]
            where = ".".join(map(str, e.path))
            raise validation(f"invalid arguments{' at ' + where if where else ''}: {e.message}")
        return {"ok": True, "result": tool.handler(hub, conn, args, actor)}
    except ApiError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message, **e.extra}}
