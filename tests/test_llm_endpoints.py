import json

EPS = json.dumps([
    {"name": "a", "base_url": "http://a.local/v1", "model": "ma", "api_key": "secret-a"},
    {"name": "b", "base_url": "http://b.local/v1", "model": "mb"},
    {"name": "bad"},
])


def test_default_and_select(mk):
    c = mk(llm_endpoints=EPS).test_client()
    r = c.get("/api/settings/llm").get_json()
    assert [e["name"] for e in r["endpoints"]] == ["a", "b"]
    assert "secret-a" not in json.dumps(r)
    assert r["selected"] == {"tidy": "a", "capture": "a"}
    r = c.put("/api/settings/llm", json={"tidy": "b"}).get_json()
    assert r["selected"] == {"tidy": "b", "capture": "a"}
    assert c.get("/health").get_json()["llm"]["model"] == "mb"
    assert c.get("/health").get_json()["capture"]["model"] == "ma"


def test_unknown_rejected(mk):
    c = mk(llm_endpoints=EPS).test_client()
    assert c.put("/api/settings/llm", json={"capture": "zzz"}).status_code == 400


def test_no_endpoints(mk):
    c = mk().test_client()
    assert c.get("/api/settings/llm").get_json()["endpoints"] == []
