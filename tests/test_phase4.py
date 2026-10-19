import io
import os
import threading
import zipfile

import pytest
from werkzeug.serving import make_server

from hub import embeddings


def _note(client, content, **kw):
    r = client.post("/api/notes", json={"content": content, **kw})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["note"] if "note" in r.get_json() else r.get_json()


def test_mirror_rebuild(app, client, tmp_path):
    n = _note(client, "alpha beta gamma")
    nid = n["id"]
    for p in (tmp_path / "notes").rglob("*.md"):
        p.unlink()
    r = client.post("/api/admin/mirror/rebuild")
    assert r.status_code == 200 and r.get_json()["notes"] == 1
    files = list((tmp_path / "notes").rglob("*.md"))
    assert len(files) == 1 and nid in files[0].read_text()


def test_export_zip(client):
    _note(client, "export me please")
    client.post("/api/entities", json={"type": "memory", "data": {
        "title": "t", "description": "d", "date": "2024-01-02"}})
    r = client.get("/api/admin/export")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert "entities.jsonl" in names and "entity_types.json" in names
    assert any(n.startswith("notes/") and n.endswith(".md") for n in names)
    assert len(z.read("entities.jsonl").splitlines()) == 1


def test_import_scan(client, tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir()
    (inbox / "_processed").mkdir()
    (inbox / "a.md").write_text("---\ntitle: Fronty\ntags: [x, y]\nhappened_at: 2024-03-01\n---\nbody one\n")
    (inbox / "sub" / "b.txt").write_text("# Heading\n\nbody two\n")
    (inbox / "c.txt").write_text("plain three")
    (inbox / ".hidden.md").write_text("nope")
    (inbox / "_processed" / "old.md").write_text("old")
    _note(client, "plain three")
    r = client.post("/api/import/scan", json={}).get_json()
    assert r["created"] == 2 and r["skipped"] == 1 and r["errors"] == []
    notes = client.get("/api/notes?limit=50").get_json()["items"]
    by = {n["title"]: n for n in notes}
    assert by["Fronty"]["tags"] == ["x", "y"] and by["Fronty"]["happened_at"] == "2024-03-01"
    assert by["Fronty"]["source"] == "import" and "Heading" in by
    assert not (inbox / "a.md").exists() and (inbox / "c.txt").exists()
    assert (inbox / ".hidden.md").exists() and (inbox / "_processed" / "old.md").exists()
    assert list((inbox / "_processed").glob("*/sub/b.txt"))
    assert client.post("/api/import/scan", json={}).get_json()["created"] == 0
    assert client.post("/api/import/scan", json={"preprocess": True}).status_code == 409


def test_reindex_model_change(mk, client, app, tmp_path):
    n = _note(client, "reindex target text")
    hub = app.extensions["hub"]
    hub.needs_reindex = True
    assert client.post("/api/notes", json={"content": "blocked"}).status_code == 409
    r = client.post("/api/admin/reindex")
    assert r.status_code == 202
    for _ in range(100):
        s = client.get("/api/admin/reindex").get_json()
        if s["job"]["state"] != "running":
            break
        threading.Event().wait(0.1)
    assert s["job"]["state"] == "done" and s["needs_reindex"] is False
    hits = client.get("/api/search", query_string={"q": "reindex target"}).get_json()
    assert n["id"] in str(hits)
    assert client.post("/api/notes", json={"content": "unblocked now"}).status_code == 201


def test_openai_embedder(monkeypatch):
    calls = []

    class Emb:
        def __init__(self, v):
            self.embedding = v

    class Resp:
        def __init__(self, n):
            self.data = [Emb([1.0, 2.0, 2.0]) for _ in range(n)]

    class Client:
        def __init__(self, **kw):
            calls.append(kw)
            self.embeddings = self

        def create(self, model, input):
            return Resp(len(input))

    import openai
    monkeypatch.setattr(openai, "OpenAI", Client)
    e = embeddings.Embedder("openai", "m", "", "http://x/v1", "k")
    assert e.dim == 3 and calls[0]["base_url"] == "http://x/v1"
    v = e.embed_documents(["a", "b"])
    assert v.shape == (2, 3) and abs(float((v[0] ** 2).sum()) - 1) < 1e-5


def test_mcp_bridge(app):
    pytest.importorskip("mcp")
    import json
    import sys

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    created = app.test_client().post("/api/entity-types", json={
        "name": "mcp_type",
        "json_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
    })
    assert created.status_code == 201
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "hub_mcp"],
        env={"HUB_URL": f"http://127.0.0.1:{srv.server_port}", "PATH": os.environ["PATH"]})

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as sess:
            await sess.initialize()
            mcp_tools = (await sess.list_tools()).tools
            names = {t.name for t in mcp_tools}
            res = await sess.call_tool("search_knowledge", {"q": "anything"})
            bad = await sess.call_tool("no_such_tool", {})
            bad_args = await sess.call_tool("create_note", {})
            return names, mcp_tools, res, bad, bad_args

    try:
        names, mcp_tools, res, bad, bad_args = anyio.run(go)
    finally:
        srv.shutdown()
    assert {"search_knowledge", "create_note", "get_self"} <= names
    extend = next(tool for tool in mcp_tools if tool.name == "extend_entity_type")
    assert extend.input_schema["properties"]["add_properties"]["type"] == "object"
    assert not res.is_error and isinstance(json.loads(res.content[0].text), (dict, list))
    assert bad.is_error
    assert bad_args.is_error
