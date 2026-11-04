"""The Claude Code provider (see bugsbot.providers.base.Provider)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bugsbot.providers.base import WorkerRequest
from bugsbot.providers.claude import credentials, remote_control
from bugsbot.providers.claude.agent import ClaudeAgent
from bugsbot.store import Store


class ClaudeProvider:
    name = "claude"
    label = "Claude Code"
    credential_setting = "anthropic_api_key"
    agent = ClaudeAgent
    worker_label = "Claude Code Remote Control"
    worker_needs_repo = False
    worker_needs_task = False

    async def check_credential(self, store: Store, value: str) -> dict[str, Any]:
        if not value:
            return {"ok": None, "detail": "not set: Claude Code's own login on this host will be used"}
        model = store.get_setting("model")
        problem = await credentials.check_api_key(value, model)
        return {"ok": problem is None, "detail": problem or f"works with {model}"}

    def login_hint(self, text: str) -> str | None:
        return credentials.LOGIN_HINT if credentials.needs_login(text) else None

    async def worker_readiness(self, store: Store, home: Path) -> dict[str, Any]:
        account = await remote_control.claude_account(store, home)
        if not account["ok"]:
            account["detail"] = (f"Claude Code on this host is {account['detail']}. As the OS user running bugsbot, "
                                 "run `claude`, then /login with your Claude account.")
        return account

    def start_worker(self, store: Store, home: Path, run_id: int,
                     request: WorkerRequest) -> remote_control.RemoteWorker:
        return remote_control.RemoteWorker(store, home, run_id, request)

    def recover_workers(self, store: Store, home: Path) -> list[remote_control.RemoteWorker]:
        remote_control.recover_workers(store, home)
        return []  # Remote Control sessions die with bugsbot, so none resume
