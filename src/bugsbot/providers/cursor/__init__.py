"""Cursor.

- `agent`: issue/PR runs through Cursor's headless CLI on this host.
- `guardrails`: bugsbot's tools and command check as project-local Cursor config.
- `cloud`: remote workers, as Cursor Cloud Agents running in Cursor's cloud.
- `api`: a client for Cursor's Cloud Agents API.
- `credentials`: Cursor API key checks, the CLI's location, and login hints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bugsbot.providers.base import AgentJob, AgentOutcome, WorkerRequest
from bugsbot.providers.cursor import agent, cloud, credentials
from bugsbot.store import Store


class CursorProvider:
    name = "cursor"
    label = "Cursor"
    credential_setting = "cursor_api_key"
    worker_label = "Cursor Cloud Agent"
    worker_needs_repo = True
    worker_needs_task = True

    async def run_agent(self, job: AgentJob) -> AgentOutcome:
        return await agent.run_agent(job)

    async def check_credential(self, store: Store, value: str) -> dict[str, Any]:
        cli = "" if credentials.find_cli() else (
            " The Cursor CLI (needed for issue/PR runs) isn't installed on this host.")
        if not value:
            return {"ok": None, "detail": "not set: Cursor CLI runs use `agent login` on this host, "
                                          f"and Cloud Agents can't start.{cli}"}
        problem = await credentials.check_api_key(value)
        return {"ok": problem is None, "detail": (problem or "accepted by Cursor") + cli}

    def login_hint(self, text: str) -> str | None:
        return credentials.LOGIN_HINT if credentials.needs_login(text) else None

    async def worker_readiness(self, store: Store, home: Path) -> dict[str, Any]:
        key = store.get_setting("cursor_api_key")
        if not key:
            return {"ok": False, "detail": "no Cursor API key is set (Settings → Credentials)"}
        problem = await credentials.check_api_key(key)
        return {"ok": problem is None, "detail": problem or "ready: agents run on the API key's Cursor account"}

    def start_worker(self, store: Store, home: Path, run_id: int, request: WorkerRequest) -> cloud.CloudWorker:
        return cloud.CloudWorker(store, home, run_id, request)

    def recover_workers(self, store: Store, home: Path) -> list[cloud.CloudWorker]:
        return cloud.recover_workers(store, home)
