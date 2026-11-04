"""What every agent provider implements, and the data passed between it and the harness.

A provider does two jobs:
- **Agent runs**: work one issue or PR autonomously (`run_agent`), with bugsbot's tools and rules.
- **Remote workers**: start an agent a maintainer drives from the provider's own app
  (`start_worker`). bugsbot starts and stops workers but doesn't control them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bugsbot.agent.toolspec import ToolSpec
from bugsbot.store import Store

Log = Callable[[str, str], None]  # (event type, text)


@dataclass
class AgentJob:
    store: Store
    home: Path
    run_id: int
    cwd: Path
    prompt: str
    rules: str            # bugsbot's hard rules, for the provider's system prompt or equivalent
    tools: list[Any]      # bugsbot's tools, for providers that run in-process
    tool_spec: ToolSpec   # the same tools, for providers that need a tool server (mcp_stdio)
    log: Log

    @property
    def settings(self) -> dict[str, Any]:
        return self.store.settings()


@dataclass
class AgentOutcome:
    ok: bool
    result: str = ""          # the agent's final message
    error: str = ""
    num_turns: int | None = None
    cost_usd: float | None = None


@dataclass
class WorkerRequest:
    repo: str        # '' for a scratch worker (where the provider supports it)
    branch: str      # '' lets the agent (or provider) pick
    label: str       # the name the maintainer typed, if any
    session_name: str
    task: str = ""   # first message, for providers that need one


class Worker(Protocol):
    run_id: int
    name: str
    link: str | None
    task: asyncio.Task

    def stop(self, reason: str = ...) -> None:
        """End the worker: bugsbot will no longer run or track it."""

    def detach(self) -> None:
        """bugsbot is shutting down: stop tracking, ending the worker only if it can't outlive bugsbot."""


class Provider(Protocol):
    name: str                 # stored with each run, e.g. 'claude'
    label: str                # shown in the UI
    credential_setting: str   # the settings key of its API key
    worker_label: str         # what its remote workers are called in the UI
    worker_needs_repo: bool
    worker_needs_task: bool

    async def run_agent(self, job: AgentJob) -> AgentOutcome: ...

    async def check_credential(self, store: Store, value: str) -> dict[str, Any]:
        """{'ok': True | False | None, 'detail': str} for an API key value ('' = not set)."""

    def login_hint(self, text: str) -> str | None:
        """Advice to show when an agent's output says it couldn't authenticate, else None."""

    async def worker_readiness(self, store: Store, home: Path) -> dict[str, Any]:
        """{'ok': bool, 'detail': str, ...}: whether a worker can start now, and as whom."""

    def start_worker(self, store: Store, home: Path, run_id: int, request: WorkerRequest) -> Worker: ...

    def recover_workers(self, store: Store, home: Path) -> list[Worker]:
        """At start-up: clean up after workers a previous bugsbot left, and return any that resume."""
