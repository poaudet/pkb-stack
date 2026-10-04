"""Hybrid search: vector kNN + FTS5 keyword, fused with RRF."""
from __future__ import annotations

import json
import re
import sqlite3

from .core import Hub
from .errors import validation

RRF_K = 60


def fts_query(q: str) -> str | None:
    toks = [t for t in re.findall(r"\w+", q, re.UNICODE) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in toks[:24]) or None


def _snippet(text: str, q: str) -> str:
    if len(text) <= 400:
        return text
    low = text.lower()
    pos = [i for t in re.findall(r"\w+", q.lower()) if len(t) > 1 and (i := low.find(t)) >= 0]
    start = max(0, min(pos) - 100) if pos else 0
    end = start + 400
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def _run(hub: Hub, conn, q, qvec, k, p) -> list[dict]:
    vec_hits: dict[int, float] = {}
    if qvec is not None:
        for cid, dist in hub.store.knn(conn, qvec, k):
            vec_hits[cid] = 1.0 - (dist * dist) / 2.0
    kw_ids: list[int] = []
    fq = fts_query(q)
    if fq:
        kw_ids = [r[0] for r in conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts, 4.0, 1.0) LIMIT 50", (fq,))]
    scores: dict[int, float] = {}
    for rank, cid in enumerate(vec_hits, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, cid in enumerate(kw_ids, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    if not scores:
        return []
    ids = list(scores)
    marks = ",".join("?" * len(ids))
    owners: list[dict] = []
    for r in conn.execute(
        "SELECT c.id AS cid, c.text, n.* FROM chunks c JOIN notes n "
        f"ON c.owner_kind='note' AND c.owner_id=n.id WHERE c.id IN ({marks})", ids
    ):
        owners.append({
            "key": ("note", r["id"]), "cid": r["cid"], "text": r["text"], "uid": r["uid"],
            "kind": "note", "type": None, "title": r["title"], "status": r["status"],
            "tags": json.loads(r["tags"]), "date": (r["happened_at"] or r["created_at"])[:10],
            "updated_at": r["updated_at"]})
    for r in conn.execute(
        "SELECT c.id AS cid, c.text, c.title AS ctitle, e.* FROM chunks c JOIN entities e "
        f"ON c.owner_kind='entity' AND c.owner_id=e.id WHERE c.id IN ({marks})", ids
    ):
        data = json.loads(r["data"])
        etags = data.get("tags")
        d = data.get("date")
        owners.append({
            "key": ("entity", r["id"]), "cid": r["cid"], "text": r["text"], "uid": r["uid"],
            "kind": "entity", "type": r["type"], "title": r["ctitle"], "status": r["status"],
            "tags": [t.lower() for t in etags if isinstance(t, str)]
            if isinstance(etags, list) else [],
            "date": (d if isinstance(d, str) and len(d) >= 10 else r["created_at"])[:10],
            "updated_at": r["updated_at"]})
    kw_set = set(kw_ids)
    best: dict[tuple, dict] = {}
    for r in owners:
        if r["status"] != "current" and not p["include_superseded"]:
            continue
        if p["kinds"] is not None and r["kind"] not in p["kinds"]:
            continue
        if p["types"] and (r["kind"] != "entity" or r["type"] not in p["types"]):
            continue
        tags = r["tags"]
        if p["tags"] and not all(t in tags for t in p["tags"]):
            continue
        d = r["date"]
        if (p["since"] and d < p["since"][:10]) or (p["until"] and d > p["until"][:10]):
            continue
        cid = r["cid"]
        sim = vec_hits.get(cid)
        if p["min_similarity"] is not None and cid not in kw_set and (
            sim is None or sim < p["min_similarity"]
        ):
            continue
        hit = best.get(r["key"])
        if hit is None or scores[cid] > hit["score"]:
            prev_sim = hit["similarity"] if hit else None
            prev_m = set(hit["matched"]) if hit else set()
            hit = {
                "id": r["uid"], "kind": r["kind"], "title": r["title"],
                "snippet": _snippet(r["text"], q), "score": scores[cid],
                "similarity": sim, "matched": prev_m, "status": r["status"],
                "tags": tags, "updated_at": r["updated_at"],
            }
            if r["type"]:
                hit["type"] = r["type"]
            if prev_sim is not None and (sim is None or prev_sim > sim):
                hit["similarity"] = prev_sim
            best[r["key"]] = hit
        else:
            if sim is not None and (hit["similarity"] is None or sim > hit["similarity"]):
                hit["similarity"] = sim
        if cid in vec_hits:
            best[r["key"]]["matched"].add("vector")
        if cid in kw_set:
            best[r["key"]]["matched"].add("keyword")
    out = sorted(best.values(), key=lambda h: -h["score"])
    for h in out:
        h["matched"] = sorted(h["matched"], reverse=True)  # vector before keyword
    return out


def search(hub: Hub, conn: sqlite3.Connection, q: str, *, limit: int = 10, kinds=None,
           tags=None, include_superseded: bool = False, min_similarity=None,
           since=None, until=None, types=None) -> dict:
    if not isinstance(q, str) or not q.strip():
        raise validation("q is required")
    if len(q) > 2000:
        raise validation("query exceeds 2000 characters")
    limit = max(1, min(int(limit), 200))
    if kinds is not None and (
        not isinstance(kinds, list) or any(k not in ("note", "entity") for k in kinds)
    ):
        raise validation("kinds must be a list of 'note'/'entity'")
    if types is not None and (not isinstance(types, list) or any(
            not isinstance(t, str) for t in types)):
        raise validation("types must be a list of strings")
    tags = [t.strip().lower() for t in (tags or []) if isinstance(t, str)]
    p = {"kinds": kinds, "types": types, "tags": tags, "include_superseded": bool(include_superseded),
         "min_similarity": min_similarity, "since": since, "until": until}
    mode = "keyword" if hub.needs_reindex else "hybrid"
    qvec = None if hub.needs_reindex else hub.embedder.embed_query(q)
    k = max(50, limit * 8)
    hits = _run(hub, conn, q, qvec, k, p)
    if len(hits) < limit and k < 500 and qvec is not None:
        hits = _run(hub, conn, q, qvec, min(500, k * 2), p)
    return {"mode": mode, "hits": hits[:limit]}
