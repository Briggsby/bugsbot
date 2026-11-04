"""A minimal client for Cursor's Cloud Agents API (v1): https://cursor.com/docs/cloud-agent/api/endpoints"""

from __future__ import annotations

from typing import Any

import httpx

API_BASE = "https://api.cursor.com"


class CursorAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Cursor API error {status}: {message}")
        self.status = status


class CursorAPI:
    def __init__(self, api_key: str, base_url: str = API_BASE, transport: httpx.AsyncBaseTransport | None = None):
        # User API keys authenticate as the HTTP Basic username, with an empty password.
        self._client = httpx.AsyncClient(base_url=base_url, auth=(api_key, ""), timeout=30,
                                         headers={"User-Agent": "bugsbot"}, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._client.request(method, path, **kw)
        if resp.status_code >= 400:
            raise CursorAPIError(resp.status_code, resp.text[:500])
        return resp.json() if resp.content else None

    async def list_agents(self, limit: int = 1) -> dict[str, Any]:
        return await self._req("GET", "/v1/agents", params={"limit": limit})

    async def create_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        """Returns {'agent': {...}, 'run': {...}}."""
        return await self._req("POST", "/v1/agents", json=body)

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/v1/agents/{agent_id}")

    async def get_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/v1/agents/{agent_id}/runs/{run_id}")

    async def cancel_run(self, agent_id: str, run_id: str) -> None:
        await self._req("POST", f"/v1/agents/{agent_id}/runs/{run_id}/cancel")

    async def archive_agent(self, agent_id: str) -> None:
        await self._req("POST", f"/v1/agents/{agent_id}/archive")
