"""Owner ("self") entity lookup, repair, and configuration."""
from __future__ import annotations

import json
import logging

from . import entities
from .db import get_meta, set_meta, tx
from .errors import ApiError, validation

log = logging.getLogger(__name__)


def _owners(conn) -> list:
    return conn.execute(
        "SELECT * FROM entities WHERE type='person' AND status='current' "
        "AND fold(json_extract(data, '$.relationship'))=fold('self') ORDER BY id"
    ).fetchall()


def reconcile(conn) -> dict:
    owners = _owners(conn)
    pointed = get_meta(conn, "self_uid")
    owner = owners[0] if owners else None
    if owner is None:
        conn.execute("DELETE FROM meta WHERE key='self_uid'")
    elif pointed != owner["uid"]:
        set_meta(conn, "self_uid", owner["uid"])
    conflicts = [row["uid"] for row in owners[1:]]
    if conflicts:
        log.warning("multiple current owner records; keeping oldest id=%s conflicts=%s",
                    owner["uid"], conflicts)
    return {"owner": owner, "conflicts": conflicts}


def get_self(conn) -> dict:
    state = reconcile(conn)
    owner = entities.to_dict(conn, state["owner"]) if state["owner"] else None
    return {"self": owner, "configured": owner is not None, "conflicts": state["conflicts"]}


def set_self(hub, conn, name: object, data: object | None = None,
             actor: str = "ui") -> dict:
    if not isinstance(name, str) or not name.strip():
        raise validation("name must be a non-empty string")
    if data is not None and not isinstance(data, dict):
        raise validation("data must be an object")
    state = reconcile(conn)
    merged = dict(data or {})
    merged["name"] = name.strip()
    merged["relationship"] = "self"
    with tx(conn):
        owner = state["owner"]
        if owner:
            result = entities.patch_entity(
                hub, conn, owner["uid"], merged, actor, in_tx=True,
                audit=False, owner_internal=True,
            )
            uid = result["id"]
        else:
            result = entities.create_entity(
                hub, conn, "person", merged, actor, in_tx=True, audit=False,
                owner_internal=True,
            )
            uid = result["id"]
        reconcile(conn)
        conn.execute(
            "INSERT INTO audit_log(actor,action,target_kind,target_uid,detail) "
            "VALUES(?,?,?,?,?)",
            (actor, "self_set", "entity", uid, json.dumps({"name": name.strip()})),
        )
    return {"self": entities.get_entity(conn, uid)}


def startup(hub) -> None:
    conn = hub.conn()
    try:
        state = reconcile(conn)
        if state["owner"] is None and hub.settings.owner_name.strip():
            set_self(hub, conn, hub.settings.owner_name, actor="system")
            log.info("configured owner record from OWNER_NAME")
    finally:
        conn.close()


def assert_change(conn, old_row, new_data: dict, *, force: bool = False) -> None:
    old_is_owner = bool(
        old_row and old_row["type"] == "person"
        and (conn.execute(
            "SELECT fold(json_extract(?, '$.relationship'))=fold('self')",
            (old_row["data"],),
        ).fetchone()[0])
    )
    new_is_owner = (new_data.get("relationship") is not None
                    and isinstance(new_data.get("relationship"), str)
                    and conn.execute("SELECT fold(?)=fold('self')",
                                     (new_data["relationship"],)).fetchone()[0])
    if old_is_owner and not new_is_owner and not force:
        raise ApiError("protected", "the owner relationship is protected", 409)
    if new_is_owner and not old_is_owner:
        row = conn.execute(
            "SELECT uid FROM entities WHERE type='person' AND status='current' "
            "AND fold(json_extract(data, '$.relationship'))=fold('self') "
            "AND id IS NOT ? ORDER BY id LIMIT 1",
            (old_row["id"] if old_row else None,),
        ).fetchone()
        if row:
            raise ApiError("conflict", "a current owner already exists", 409,
                           existing=row["uid"])
