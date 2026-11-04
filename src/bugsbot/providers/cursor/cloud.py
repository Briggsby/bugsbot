"""Cursor remote workers: Cursor Cloud Agents, launched through Cursor's API.

A cloud agent runs on Cursor's machines against a GitHub repository, and a maintainer chats
with it at cursor.com/agents (or Cursor's apps). bugsbot launches it with the maintainer's
first message, links to it, and stops it on request (cancels its run and archives it).

bugsbot doesn't control it after launch, and doesn't record its conversation: it only polls
the agent's status and logs the branches and PRs it reports.

Because it runs outside this host, **bugsbot's guardrails don't apply**: no command hook,
no signed tools, no credential isolation. It pushes with Cursor's GitHub app, so it can do
whatever that app is allowed to on the repo. bugsbot's rules are only given to it as
instructions. Protect the default branch with a ruleset if that matters.

Cloud agents outlive bugsbot: when bugsbot restarts, it resumes tracking them.
"""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Any

from bugsbot.github import GitHub, resolve_token
from bugsbot.providers.base import WorkerRequest
from bugsbot.providers.cursor.api import CursorAPI, CursorAPIError
from bugsbot.store import Store

POLL_SECONDS = 20
TERMINAL_RUN = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}


def worker_rules(signature: str, default_branch: str, branch: str) -> str:
    where = (f"Work on the branch `{branch}`." if branch else
             "Cursor gives you a new branch for this task; work there.")
    return f"""

---
Rules from bugsbot, the tool that started you. A maintainer will chat with you in Cursor.
- Never merge a pull request, by any means. A human always merges.
- Never push to `{default_branch}`, `main` or `master`. {where}
- End every GitHub comment and pull request description you write with this signature:

---
{signature}
"""


class CloudWorker:
    def __init__(self, store: Store, home: Path, run_id: int, request: WorkerRequest | None = None,
                 resume: dict[str, Any] | None = None):
        """Launch a new agent for `request`, or resume tracking the run `resume` (a runs row)."""
        self.store, self.home, self.run_id = store, home, run_id
        self.request = request
        self.agent_id: str = (resume or {}).get("external_id", "")
        self.link: str | None = (resume or {}).get("summary") or None
        self.name = ((request.label or request.session_name) if request
                     else (resume or {}).get("trigger", "").removeprefix("worker: "))
        self._end_remote = True
        self._stop_reason = "stopped from bugsbot"
        self._last_status: tuple[str, str] = ("", "")
        self.task = asyncio.create_task(self._main())

    def log(self, type_: str, text: str) -> None:
        self.store.add_event(self.run_id, type_, text)

    def stop(self, reason: str = "stopped from bugsbot") -> None:
        self._stop_reason, self._end_remote = reason, True
        self.task.cancel()

    def detach(self) -> None:
        # The agent keeps running in Cursor's cloud; the next bugsbot start resumes tracking it.
        self._stop_reason, self._end_remote = "bugsbot shut down", False
        self.task.cancel()

    async def _main(self) -> None:
        settings = self.store.settings()
        api = CursorAPI(settings["cursor_api_key"])
        status, error = "succeeded", ""
        try:
            if not settings["cursor_api_key"]:
                raise RuntimeError("no Cursor API key is set (Settings → Credentials)")
            if self.agent_id:
                self.log("info", "bugsbot restarted; resumed tracking this Cursor Cloud Agent")
            else:
                await self._launch(api, settings)
            error = await self._watch(api)
        except asyncio.CancelledError:
            if not self._end_remote:
                self.log("info", "bugsbot is shutting down; the Cursor Cloud Agent keeps running "
                                 "and bugsbot resumes tracking it when it restarts")
                await api.close()
                return  # the run stays 'running' so it can be resumed
            status, error = "cancelled", self._stop_reason
            await asyncio.shield(self._end(api))
        except Exception as e:
            status, error = "failed", f"{type(e).__name__}: {e}"
            self.log("error", traceback.format_exc())
        await api.close()
        self.store.finish_run(self.run_id, status, summary=self.link or "", error=error)

    async def _launch(self, api: CursorAPI, settings: dict[str, Any]) -> None:
        assert self.request is not None
        repo, branch = self.request.repo, self.request.branch
        gh = GitHub(resolve_token(settings["github_token"]))
        try:
            default_branch = (await gh.repo(repo))["default_branch"]
            if branch in (default_branch, "main", "master"):
                raise ValueError(f"'{branch}' is protected; name a feature branch or leave it blank")
            if branch and not await gh.branch_exists(repo, branch):
                # Cloud agents can only start from an existing branch, so create it first.
                await gh.create_branch(repo, branch, default_branch)
                self.log("info", f"Created branch {branch} from {default_branch} on GitHub")
        finally:
            await gh.close()

        body: dict[str, Any] = {
            "name": (self.request.label or self.request.session_name)[:100],
            "prompt": {"text": self.request.task + worker_rules(settings["signature"], default_branch, branch)},
            "repos": [{"url": f"https://github.com/{repo}", "startingRef": branch or default_branch}],
            # With a named branch, push to it; otherwise Cursor creates a cursor/... branch.
            "workOnCurrentBranch": bool(branch),
            "autoCreatePR": False,
        }
        if settings["cursor_model"]:
            body["model"] = {"id": settings["cursor_model"]}
        self.log("info", f"Launching a Cursor Cloud Agent on {repo} "
                         f"({'branch ' + branch if branch else 'a new branch from ' + default_branch})")
        agent = (await api.create_agent(body))["agent"]
        self.agent_id = agent["id"]
        self.store.set_run_external_id(self.run_id, self.agent_id)
        self.link = agent.get("url") or f"https://cursor.com/agents?id={self.agent_id}"
        self.store.set_run_summary(self.run_id, self.link)
        self.log("tool_ok", f"Cursor Cloud Agent started: {self.link}")
        self.log("info", "From here on the agent is controlled from Cursor; bugsbot only tracks its status "
                         "(and can stop it).")

    async def _watch(self, api: CursorAPI) -> str:
        """Poll until the agent is archived or deleted in Cursor. Returns the reason it ended."""
        failures = 0
        while True:
            try:
                agent = await api.get_agent(self.agent_id)
                failures = 0
            except CursorAPIError as e:
                if e.status == 404:
                    self.log("info", "The agent was deleted in Cursor")
                    return ""
                failures += 1
                if failures == 1:
                    self.log("error", f"Couldn't check the agent's status: {e} (will keep trying)")
                await asyncio.sleep(POLL_SECONDS)
                continue
            if agent.get("status") == "ARCHIVED":
                self.log("info", "The agent was archived in Cursor")
                return ""
            await self._log_run(api, agent)
            await asyncio.sleep(POLL_SECONDS)

    async def _log_run(self, api: CursorAPI, agent: dict[str, Any]) -> None:
        run_id = agent.get("latestRunId") or ""
        try:
            run = await api.get_run(self.agent_id, run_id) if run_id else {}
        except CursorAPIError:
            return
        status = (agent.get("status", ""), run.get("status", ""))
        if status == self._last_status:
            return
        self._last_status = status
        self.log("info", f"Agent {status[0].lower() or '?'}, latest run {status[1].lower() or 'none'}")
        if status[1] in TERMINAL_RUN:
            for b in (run.get("git") or {}).get("branches") or []:
                parts = [f"branch {b['branch']}" if b.get("branch") else "", b.get("prUrl") or ""]
                self.log("tool_ok", f"{b.get('repoUrl', '')}: {' '.join(p for p in parts if p)}")

    async def _end(self, api: CursorAPI) -> None:
        """Stop the agent's current run and archive it (archiving is reversible in Cursor)."""
        if not self.agent_id:
            return
        try:
            agent = await api.get_agent(self.agent_id)
            if run_id := agent.get("latestRunId"):
                try:
                    await api.cancel_run(self.agent_id, run_id)
                except CursorAPIError as e:
                    if e.status != 409:  # 409: the run had already finished
                        raise
            await api.archive_agent(self.agent_id)
            self.log("info", "Stopped and archived the Cursor Cloud Agent")
        except CursorAPIError as e:
            if e.status != 404:
                self.log("error", f"Couldn't stop the Cursor Cloud Agent: {e}. Stop it at {self.link}")


def recover_workers(store: Store, home: Path) -> list[CloudWorker]:
    return [CloudWorker(store, home, run["id"], resume=run) for run in store.resumable_workers("cursor")]
