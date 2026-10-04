import io
import json
import time
import zipfile

import pytest

from hub.embeddings import Embedder
from tests.conftest import make_settings


def _note(client, content, **kw):
    r = client.post("/api/notes", json={"content": content, **kw})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["note"]


def test_mirror_rebuild(client, tmp_path):
    n = _note(client, "Hello mirror world", title="Mirror")
    root = tmp_path / "notes"
    for p in root.rglob("*.md"):
        p.unlink()
    r = client.post("/api/admin/mirror/rebuild")
    assert r.status_code == 200 and r.get_json()["notes"] == 1
    assert list(root.rglob(f"*-{n['id'][:8]}.md"))


def test_export_zip(client):
    _note(client, "Exported note body", title="Exp")
    client.post("/api/entities", json={"type": "person", "data": {"name": "Ana"}})
    r = client.get("/api/admin/export")
    assert r.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert "entities.jsonl" in names and "entity_types.json" in names
    assert any(n.startswith("notes/") for n in names)
    rows = [json.loads(x) for x in z.read("entities.jsonl").decode().splitlines()]
    assert len(rows) == 1


def test_import_scan(client, tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "a.md").write_text("---\ntitle: From FM\ntags: [x]\n---\nBody one\n")
    (inbox / "sub" / "b.txt").write_text("# Heading\nBody two\n")
    (inbox / ".hidden.md").write_text("ignored")
    (inbox / "c.md").write_text("Body one")  # duplicate of a.md
    r = client.post("/api/import/scan", json={})
    d = r.get_json()
    assert r.status_code == 200
    assert d["imported"] == 2 and d["skipped"] == 1 and d["errors"] == 0
    assert (inbox / ".hidden.md").exists() and not (inbox / "a.md").exists()
    assert len(list((inbox / "_processed").rglob("*.*"))) == 3
    items = client.get("/api/notes").get_json()["items"]
    assert {i["title"] for i in items} == {"From FM", "Heading"}
    assert all(i["source"] == "import" for i in items)
    again = client.post("/api/import/scan", json={}).get_json()
    assert again["imported"] == 0 and again["files"] == []


def test_import_preprocess_needs_llm(client, tmp_path):
    (tmp_path / "inbox" / "a.md").write_text("x y z")
    assert client.post("/api/import/scan", json={"preprocess": True}).status_code == 409
    assert (tmp_path / "inbox" / "a.md").exists()


def test_reindex_after_model_change(tmp_path, mk):
    import hub as hubpkg

    app = mk()
    _note(app.test_client(), "Alpha beta gamma", title="One")
    app2 = hubpkg.create_app(
        make_settings(tmp_path), embedder=_Other())
    c = app2.test_client()
    assert c.get("/health").get_json()["status"] == "needs_reindex"
    assert c.post("/api/notes", json={"content": "zzz"}).status_code == 409
    assert c.post("/api/admin/reindex").status_code == 202
    for _ in range(100):
        s = c.get("/api/admin/reindex").get_json()
        if s["state"] != "running":
            break
        time.sleep(0.05)
    assert s["state"] == "done", s
    assert c.get("/health").get_json()["status"] == "ok"
    hits = c.get("/api/search", query_string={"q": "alpha"}).get_json()
    assert len(hits["hits"]) == 1


class _Other(Embedder):
    def __init__(self):
        super().__init__("fake", "fake")
        self.model = "other-fake"


def test_openai_embedder(monkeypatch):
    import openai

    class R:
        def __init__(self, n):
            self.data = [type("D", (), {"embedding": [1.0, 0.0, float(i)]})() for i in range(n)]

    class FakeClient:
        def __init__(self, **kw):
            self.embeddings = self

        def create(self, model, input):
            assert model == "m"
            return R(len(input))

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    e = Embedder("openai", "m", base_url="http://x", api_key="k")
    assert e.dim == 3
    v = e.embed_documents(["a", "b"])
    assert v.shape == (2, 3)
    assert e.embed_query("q").shape == (3,)


def test_unknown_backend():
    with pytest.raises(ValueError):
        Embedder("nope", "m")


