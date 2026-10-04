import json

import httpx

from hub_mcp.bridge import BridgeConfig, HubClient


def _client(handler):
    cfg = BridgeConfig("http://hub", "tok", "agent:mcp")
    return HubClient(cfg, transport=httpx.MockTransport(handler))


def test_list_and_call():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["actor"] = req.headers["x-actor"]
        if req.url.path == "/api/tools":
            return httpx.Response(200, json=[{"type": "function", "function": {
                "name": "t", "description": "d", "parameters": {"type": "object"}}}])
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "result": 1})

    c = _client(handler)
    assert c.list_tools()[0]["name"] == "t"
    body, err = c.call_tool("t", {"a": 1})
    assert body["ok"] and not err and seen["body"] == {"a": 1}
    assert seen["auth"] == "Bearer tok" and seen["actor"] == "agent:mcp"


def test_errors():
    c = _client(lambda r: httpx.Response(200, json={"ok": False, "error": {"code": "x"}}))
    assert c.call_tool("t", None)[1] is True
    c = _client(lambda r: httpx.Response(404, json={"error": {"code": "not_found"}}))
    body, err = c.call_tool("t", {})
    assert err and body["error"]["code"] == "not_found"
    c = _client(lambda r: httpx.Response(502, text="bad gateway"))
    assert c.call_tool("t", {})[1] is True
