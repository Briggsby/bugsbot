"""Ad-hoc interactive agent chats opened from the web UI.

Each chat is a long-lived Claude Agent SDK session (one Claude Code worker process)
working in its own checkout. Chats are recorded as runs of kind 'chat', so they show
up with their duration, turns and cost next to the issue/PR runs.
"""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, ResultMessage

from bugsbot.agent.policy import SYSTEM_APPEND
from bugsbot.agent.session import build_options, log_message
from bugsbot.agent.tools import ToolKit
from bugsbot.github import GitHub, resolve_token
from bugsbot.store import Store
from bugsbot.workspace import Workspace

CHAT_APPEND = SYSTEM_APPEND + """
You are in an interactive chat with a maintainer through the bugsbot web UI. Do what they
ask, keep them informed, and ask when something is ambiguous. When you open PRs or post
comments, use the bugsbot tools.
"""


class ChatSession:
    def __init__(self, store: Store, home: Path, run_id: int, repo: str, branch: str):
        self.store, self.home, self.run_id = store, home, run_id
        self.repo, self.branch = repo, branch
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.busy = False
        self.client: ClaudeSDKClient | None = None
        self.task = asyncio.create_task(self._main())

    def log(self, type_: str, text: str) -> None:
        self.store.add_event(self.run_id, type_, text)

    def send(self, text: str) -> None:
        self.log("user", text)
        self.queue.put_nowait(text)

    async def interrupt(self) -> None:
        if self.client and self.busy:
            await self.client.interrupt()

    def close(self) -> None:
        self.queue.put_nowait(None)

    async def _main(self) -> None:
        settings = self.store.settings()
        gh: GitHub | None = None
        status, error = "succeeded", ""
        try:
            tools: list[Any] = []
            if self.repo:
                gh = GitHub(resolve_token(settings["github_token"]))
                info = await gh.repo(self.repo)
                default_branch = info["default_branch"]
                branch = self.branch or f"bugsbot/chat-{self.run_id}"
                workspace = Workspace(self.home, self.repo, "chat", self.run_id, gh.token)
                self.log("info", f"Preparing checkout of {self.repo} on branch {branch}")
                await workspace.prepare(branch, default_branch, settings["git_author_name"],
                                        settings["git_author_email"])
                cwd = workspace.path
                kit = ToolKit(gh=gh, repo=self.repo, signature=settings["signature"], workspace=workspace,
                              protected={default_branch, "main", "master"}, base_branch=default_branch,
                              log=self.log)
                tools = [kit.push_branch(), kit.open_pull_request(labels=None), kit.comment()]
            else:
                cwd = self.home / "chats" / f"chat-{self.run_id}"
                cwd.mkdir(parents=True, exist_ok=True)
            self.log("info", f"Worker ready in {cwd}")
            options = build_options(settings, self.home, cwd, CHAT_APPEND, tools, self.log, max_turns=None)
            async with ClaudeSDKClient(options=options) as client:
                self.client = client
                while (text := await self.queue.get()) is not None:
                    self.busy = True
                    try:
                        await client.query(text)
                        async for msg in client.receive_response():
                            log_message(self.log, msg)
                            if isinstance(msg, ResultMessage):
                                self.store.add_run_usage(self.run_id, msg.num_turns, msg.total_cost_usd or 0)
                    finally:
                        self.busy = False
        except asyncio.CancelledError:
            status, error = "cancelled", "chat terminated"
        except Exception as e:
            status, error = "failed", f"{type(e).__name__}: {e}"
            self.log("error", traceback.format_exc())
        finally:
            self.client = None
            if gh:
                await gh.close()
            self.store.finish_run(self.run_id, status, summary="chat closed" if status == "succeeded" else "",
                                  error=error)


class ChatManager:
    def __init__(self, store: Store, home: Path):
        self.store, self.home = store, home
        self.sessions: dict[int, ChatSession] = {}

    def open(self, repo: str, branch: str, message: str) -> int:
        title = f"chat: {message.strip().splitlines()[0][:80]}" if message.strip() else "chat"
        run_id = self.store.start_chat(repo, title)
        session = ChatSession(self.store, self.home, run_id, repo, branch)
        session.task.add_done_callback(lambda _: self.sessions.pop(run_id, None))
        self.sessions[run_id] = session
        if message.strip():
            session.send(message)
        return run_id

    def get(self, run_id: int) -> ChatSession | None:
        return self.sessions.get(run_id)

    async def stop(self) -> None:
        for s in list(self.sessions.values()):
            s.task.cancel()
        await asyncio.gather(*(s.task for s in self.sessions.values()), return_exceptions=True)
