"""The Cursor provider (see bugsbot.providers.base.Provider)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bugsbot.providers.base import WorkerRequest
from bugsbot.providers.cursor import cloud, credentials
from bugsbot.providers.cursor.agent import CursorAgent
from bugsbot.store import Store


class CursorProvider:
    name = "cursor"
    label = "Cursor"
    credential_setting = "cursor_api_key"
    agent = CursorAgent
    worker_label = "Cursor Cloud Agent"
    worker_needs_repo = True
    worker_needs_task = True

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
