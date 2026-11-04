"""Remote workers: agents a maintainer drives from their provider's own app.

bugsbot starts and stops workers and records each as a run of kind 'worker', but doesn't
control them. Each provider supplies its own kind of worker (see bugsbot.providers).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from bugsbot import providers
from bugsbot.providers.base import Worker, WorkerRequest
from bugsbot.store import Store


class WorkerManager:
    def __init__(self, store: Store, home: Path):
        self.store, self.home = store, home
        self.workers: dict[int, Worker] = {}
        self.provider_of: dict[int, str] = {}

    def _track(self, run_id: int, provider: str, worker: Worker) -> None:
        worker.task.add_done_callback(lambda _: (self.workers.pop(run_id, None), self.provider_of.pop(run_id, None)))
        self.workers[run_id] = worker
        self.provider_of[run_id] = provider

    def recover(self) -> None:
        """At start-up, before runs are marked interrupted: clean up, and resume workers that outlived bugsbot."""
        for provider in providers.PROVIDERS.values():
            for worker in provider.recover_workers(self.store, self.home):
                self._track(worker.run_id, provider.name, worker)

    def open(self, provider_name: str, repo: str, branch: str, name: str, task: str = "") -> int:
        provider = providers.get(provider_name)
        label = name.strip() or (repo.split("/")[-1] if repo else "scratch")
        run_id = self.store.start_worker(repo, f"worker: {label}", provider.name)
        session_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"bugsbot-{run_id}-{label}")[:60]
        request = WorkerRequest(repo=repo, branch=branch, label=name.strip(), session_name=session_name,
                                task=task.strip())
        self._track(run_id, provider.name, provider.start_worker(self.store, self.home, run_id, request))
        return run_id

    def get(self, run_id: int) -> Worker | None:
        return self.workers.get(run_id)

    def describe(self) -> dict[int, dict[str, str | None]]:
        return {rid: {"name": w.name, "link": w.link, "provider": self.provider_of[rid]}
                for rid, w in self.workers.items()}

    async def stop(self) -> None:
        """bugsbot is shutting down."""
        for w in list(self.workers.values()):
            w.detach()
        await asyncio.gather(*(w.task for w in self.workers.values()), return_exceptions=True)
