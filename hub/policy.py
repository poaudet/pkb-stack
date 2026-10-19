"""Per-entity-type visibility and audit policy."""
from __future__ import annotations


def _flag(conn, type_name: str, column: str) -> bool:
    if column not in {"capture_visible", "agent_visible", "auditable"}:
        raise ValueError("unknown entity type policy")
    row = conn.execute(
        f"SELECT {column} FROM entity_types WHERE name=?", (type_name,)
    ).fetchone()
    return bool(row[column]) if row else False


def is_capture_visible(conn, type_name: str) -> bool:
    return _flag(conn, type_name, "capture_visible")


def is_agent_visible(conn, type_name: str) -> bool:
    return _flag(conn, type_name, "agent_visible")


def is_auditable(conn, type_name: str) -> bool:
    return _flag(conn, type_name, "auditable")


def capture_excluded_types(conn, override: str = "") -> set[str]:
    excluded = {part.strip() for part in override.split(",") if part.strip()}
    excluded.update(
        row["name"] for row in conn.execute(
            "SELECT name FROM entity_types WHERE capture_visible=0"
        )
    )
    return excluded


def require_agent_visible(conn, type_name: str) -> None:
    if not is_agent_visible(conn, type_name):
        from .errors import not_found

        raise not_found(f"entity type {type_name!r} not found")


def require_entity_agent_visible(conn, uid: str):
    row = conn.execute("SELECT type FROM entities WHERE uid=?", (uid,)).fetchone()
    if row is None or not is_agent_visible(conn, row["type"]):
        from .errors import not_found

        raise not_found("entity not found")
    return row["type"]
