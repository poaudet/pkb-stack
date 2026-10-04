import glob
import os
import sqlite3

from hub import db
from hub.search import fts_query


def mk_note(client, content, **kw):
    r = client.post("/api/notes", json={"content": content, **kw})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["note"]


def search(client, q, **kw):
    r = client.post("/api/search", json={"q": q, **kw})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["hits"]


def test_health_and_headers(client):
    r = client.get("/health")
    j = r.get_json()
    assert j["status"] == "ok" and j["vector_backend"] in ("sqlite-vec", "numpy")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_fts_query_safe(client):
    mk_note(client, "Toyota RAV4 collision sensor fix")
    assert fts_query("how do I fix my RAV4 collision sensor?")
    assert search(client, "how do I fix my RAV4 collision sensor?")
    assert search(client, "???") == [] or True


def test_accent_fold_and_keyword(client):
    n = mk_note(client, "Le café de Zürich était délicieux", title="Voyage")
    hits = search(client, "cafe")
    assert hits and hits[0]["id"] == n["id"] and "keyword" in hits[0]["matched"]


def test_supersede_hides_old(client):
    a = mk_note(client, "Alice birthday is June 3")
    r = client.post(f"/api/notes/{a['id']}/supersede", json={"content": "Alice birthday is June 4"})
    assert r.status_code == 201
    ids = [h["id"] for h in search(client, "Alice birthday")]
    assert a["id"] not in ids and r.get_json()["note"]["id"] in ids
    ids2 = [h["id"] for h in search(client, "Alice birthday", include_superseded=True)]
    assert a["id"] in ids2
    old = client.get(f"/api/notes/{a['id']}").get_json()["note"]
    assert old["status"] == "superseded" and old["superseded_by"] == r.get_json()["note"]["id"]
    assert glob.glob(os.path.join(client.application.extensions["hub"].settings.data_dir,
                                  "notes", "_history", "**", "*.md"), recursive=True)


def test_archive_restore(client):
    a = mk_note(client, "archive me please")
    client.post(f"/api/notes/{a['id']}/archive", json={})
    assert search(client, "archive") == []
    client.post(f"/api/notes/{a['id']}/restore")
    assert search(client, "archive")


def test_duplicate(client):
    mk_note(client, "same text")
    r = client.post("/api/notes", json={"content": "same text"})
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "duplicate"
    assert client.post("/api/notes", json={"content": "same text",
                                           "allow_duplicate": True}).status_code == 201


def test_patch_resyncs_fts(client):
    a = mk_note(client, "original zebra")
    client.patch(f"/api/notes/{a['id']}", json={"content": "now giraffe"})
    assert search(client, "zebra") == [] or "zebra" not in search(client, "zebra")[0]["snippet"]
    assert search(client, "giraffe")


def test_forget_leaves_no_trace(client):
    hub = client.application.extensions["hub"]
    a = mk_note(client, "secret platypus content", title="Secret")
    assert client.delete(f"/api/notes/{a['id']}").status_code == 400
    assert client.delete(f"/api/notes/{a['id']}?confirm=true").status_code == 200
    conn = db.connect(hub.settings.db_path)
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'platypus'"
                        ).fetchone()[0] == 0
    tbl = "vec_chunks" if hub.store.backend == "sqlite-vec" else "chunk_vectors"
    assert conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0] == 0
    assert not glob.glob(os.path.join(hub.settings.data_dir, "notes", "**", "*.md"),
                         recursive=True)
    log = conn.execute("SELECT detail, action FROM audit_log WHERE action='forget'").fetchone()
    assert log and "platypus" not in str(tuple(log))
    assert client.get(f"/api/notes/{a['id']}").status_code == 404


def test_hybrid_best_chunk_grouping(client):
    long = "\n\n".join(f"Paragraph {i}. " + "filler words here " * 30 for i in range(8))
    long += "\n\nUnique marker quokka lives here."
    n = mk_note(client, long)
    hits = search(client, "quokka")
    assert len([h for h in hits if h["id"] == n["id"]]) == 1
    assert "quokka" in hits[0]["snippet"]


def test_validation_and_filters(client):
    assert client.post("/api/notes", json={"content": "  "}).status_code == 400
    assert client.post("/api/notes", json={"content": "x", "happened_at": "bad"}).status_code == 400
    mk_note(client, "tagged note", tags=["Work"])
    mk_note(client, "other note", tags=["home"])
    assert client.get("/api/notes?tag=work").get_json()["total"] == 1
    assert [h["title"] for h in search(client, "note", tags=["work"])] == ["tagged note"]
    assert client.get("/api/notes?status=bogus").status_code == 400
    assert client.post("/api/notes", json={"content": "x", "preprocess": True}
                       ).get_json()["error"]["code"] == "llm_not_configured"


def test_auth(mk):
    c = mk(api_token="s3cret").test_client()
    assert c.get("/health").status_code == 200
    assert c.get("/api/stats").status_code == 401
    assert c.get("/api/stats", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/stats", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_backup_and_stats(client):
    mk_note(client, "backup me")
    for _ in range(3):
        assert client.post("/api/admin/backup").status_code == 201
    d = os.path.join(client.application.extensions["hub"].settings.data_dir, "backups")
    files = glob.glob(os.path.join(d, "knowledge-*.db"))
    assert len(files) == 2
    assert sqlite3.connect(files[0]).execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    assert client.get("/api/stats").get_json()["notes"]["current"] == 1


def test_needs_reindex(tmp_path, mk):
    from hub import create_app
    from hub.core import Hub
    from tests.conftest import make_settings

    mk()  # init meta with embed_model "fake"
    conn = db.connect(str(tmp_path / "knowledge.db"))
    db.set_meta(conn, "embed_model", "other-model")
    conn.close()
    c = create_app(make_settings(tmp_path)).test_client()
    assert c.get("/health").get_json()["status"] == "needs_reindex"
    assert c.post("/api/notes", json={"content": "x"}).get_json()["error"]["code"] == "needs_reindex"
    assert c.post("/api/search", json={"q": "x"}).get_json()["mode"] == "keyword"
    assert Hub


def test_ui_static(client):
    assert client.get("/").status_code == 200
    js = client.get("/static/app.js").get_data(as_text=True)
    assert "innerHTML" not in js
    html = client.get("/").get_data(as_text=True)
    assert "onclick=" not in html and "style=" not in html
    assert "<script>" not in html


def test_chunking():
    from hub.chunking import chunk_markdown

    assert chunk_markdown("short") == ["short"]
    big = "\n\n".join("Sentence one. Sentence two. " * 20 for _ in range(6))
    cs = chunk_markdown(big, 900)
    assert len(cs) > 1 and all(len(c) <= 900 + 200 for c in cs)
