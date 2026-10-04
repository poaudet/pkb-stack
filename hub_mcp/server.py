"""MCP bridge: exposes the hub's /api/tools as MCP tools (stdio or streamable HTTP).

Env: HUB_URL, API_TOKEN, MCP_ACTOR, MCP_TRANSPORT=stdio|http, MCP_HOST, MCP_PORT.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .bridge import BridgeConfig, HubClient

log = logging.getLogger("hub_mcp")


def build_server(client: HubClient) -> Server:
    tools = [
        types.Tool(name=t["name"], description=t.get("description", ""),
                   input_schema=t.get("parameters") or {"type": "object", "properties": {}})
        for t in client.list_tools()
    ]
    log.info("registered %d tools", len(tools))

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params):
        body, is_error = await anyio.to_thread.run_sync(
            client.call_tool, params.name, params.arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
            is_error=is_error)

    return Server("knowledge-hub", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _run_stdio(server: Server) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), stream=sys.stderr)
    server = build_server(HubClient(BridgeConfig.from_env()))
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        import uvicorn

        app = server.streamable_http_app(host=os.environ.get("MCP_HOST", "0.0.0.0"))
        uvicorn.run(app, host=os.environ.get("MCP_HOST", "0.0.0.0"),
                    port=int(os.environ.get("MCP_PORT", "8000")))
    else:
        anyio.run(_run_stdio, server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
