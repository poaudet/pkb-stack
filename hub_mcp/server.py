"""MCP bridge: exposes the hub's /api/tools manifest as MCP tools (stdio or streamable HTTP)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys

import httpx
from mcp import types
from mcp.server.lowlevel import Server

log = logging.getLogger("hub_mcp")


def load_manifest(client: httpx.Client) -> list[dict]:
    r = client.get("/api/tools")
    r.raise_for_status()
    return [t["function"] for t in r.json()]


def build_server(client: httpx.AsyncClient, manifest: list[dict], actor: str) -> Server:
    tools = [
        types.Tool(name=f["name"], description=f.get("description", ""),
                   inputSchema=f.get("parameters") or {"type": "object", "properties": {}})
        for f in manifest
    ]

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        try:
            r = await client.post(f"/api/tools/{params.name}", json=params.arguments or {},
                                  headers={"X-Actor": actor})
        except httpx.HTTPError as e:
            return _result(f"hub unreachable: {type(e).__name__}", True)
        try:
            body = r.json()
        except ValueError:
            return _result(f"hub returned HTTP {r.status_code}", True)
        is_err = r.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False)
        return _result(json.dumps(body, ensure_ascii=False), is_err)

    return Server("knowledge-hub", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _result(text: str, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], isError=is_error)


class RequireBearer:
    """ASGI middleware: reject HTTP requests lacking the shared API token."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._digest = hashlib.sha256(token.encode()).digest()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            candidate = supplied[7:] if supplied.startswith("Bearer ") else ""
            if not hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), self._digest):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _headers() -> dict[str, str]:
    token = os.environ.get("API_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), stream=sys.stderr)
    base = os.environ.get("HUB_URL", "http://127.0.0.1:5000")
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    actor = os.environ.get("MCP_ACTOR", "agent:mcp")
    with httpx.Client(base_url=base, headers=_headers(), timeout=30) as sync_client:
        manifest = load_manifest(sync_client)
    log.info("registered %d tools from %s", len(manifest), base)
    client = httpx.AsyncClient(base_url=base, headers=_headers(), timeout=120)
    server = build_server(client, manifest, actor)
    if transport == "stdio":
        import anyio
        from mcp.server.stdio import stdio_server

        async def run() -> None:
            async with stdio_server() as (r, w):
                await server.run(r, w, server.create_initialization_options())

        anyio.run(run)
        return 0
    if transport == "streamable-http":
        import uvicorn

        host = os.environ.get("MCP_HOST", "0.0.0.0")
        app = server.streamable_http_app(host=host)
        token = os.environ.get("API_TOKEN", "")
        if token:
            app = RequireBearer(app, token)
        else:
            log.warning("API_TOKEN is empty: the MCP endpoint is unauthenticated. "
                        "Do not expose it.")
        uvicorn.run(app, host=host, port=int(os.environ.get("MCP_PORT", "8000")))
        return 0
    print(f"unknown MCP_TRANSPORT {transport!r} (stdio|streamable-http)", file=sys.stderr)
    return 2
