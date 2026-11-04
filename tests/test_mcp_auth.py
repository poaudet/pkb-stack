import asyncio

import httpx

from hub_mcp.server import RequireBearer

TOKEN = "t" * 40


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _status(headers: dict[str, str]) -> int:
    async def call() -> int:
        transport = httpx.ASGITransport(app=RequireBearer(_ok, TOKEN))
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
            return (await client.post("/mcp", headers=headers)).status_code

    return asyncio.run(call())


def test_http_endpoint_requires_the_shared_token():
    assert _status({}) == 401
    assert _status({"Authorization": "Bearer wrong"}) == 401
    assert _status({"Authorization": TOKEN}) == 401
    assert _status({"Authorization": f"Bearer {TOKEN}"}) == 200
