"""HTTP client for the hub's agent-tool API (no MCP dependency)."""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class BridgeConfig:
    hub_url: str
    api_token: str
    actor: str
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> BridgeConfig:
        return cls(
            hub_url=(os.environ.get("HUB_URL") or "http://hub:5000").rstrip("/"),
            api_token=os.environ.get("API_TOKEN", ""),
            actor=os.environ.get("MCP_ACTOR") or "agent:mcp",
        )


class HubClient:
    def __init__(self, cfg: BridgeConfig, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"X-Actor": cfg.actor}
        if cfg.api_token:
            headers["Authorization"] = f"Bearer {cfg.api_token}"
        self._http = httpx.Client(base_url=cfg.hub_url, headers=headers, timeout=cfg.timeout,
                                  transport=transport)

    def list_tools(self) -> list[dict]:
        """Return [{name, description, parameters}] from GET /api/tools."""
        r = self._http.get("/api/tools")
        r.raise_for_status()
        return [t["function"] for t in r.json()]

    def call_tool(self, name: str, arguments: dict | None) -> tuple[dict, bool]:
        """POST /api/tools/{name}. Returns (response body, is_error)."""
        r = self._http.post(f"/api/tools/{name}", json=arguments or {})
        try:
            body = r.json()
        except ValueError:
            return {"ok": False, "error": {"code": "internal", "message": f"HTTP {r.status_code}"}}, True
        if r.status_code >= 400:
            return {"ok": False, **body}, True
        return body, not body.get("ok", False)
