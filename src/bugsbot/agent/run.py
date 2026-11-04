"""One autonomous agent run for one issue or PR."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, ResultMessage

from bugsbot.agent.policy import SYSTEM_APPEND
from bugsbot.anthropic_check import LOGIN_HINT, needs_login
from bugsbot.agent.prompts import issue_prompt, pr_prompt
from bugsbot.agent.session import build_options, log_message
from bugsbot.agent.tools import ToolKit
from bugsbot.github import GitHub
from bugsbot.store import Store
from bugsbot.work import Work
from bugsbot.workspace import Workspace


@dataclass
class RunContext:
    store: Store
    gh: GitHub
    home: Path
    run_id: int
    item: dict[str, Any]
    work: Work
    tracked_labels: list[str]


@dataclass
class RunResult:
    status: str  # 'succeeded' | 'failed'
    summary: str
    num_turns: int | None = None
    cost_usd: float | None = None


@dataclass
class _Plan:
    """Everything a run needs once its workspace is ready."""
    cwd: Path
    prompt: str
    tools: list[Any]


class AgentRun:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.settings = ctx.store.settings()
        self.replied: set[str] = set()
        self.opened_prs: list[int] = []

    def log(self, type_: str, text: str) -> None:
        self.ctx.store.add_event(self.ctx.run_id, type_, text)

    async def run(self) -> RunResult:
        item = self.ctx.item
        plan = await (self._plan_issue() if item["kind"] == "issue" else self._plan_pr())
        self.log("prompt", plan.prompt)
        options = build_options(self.settings, self.ctx.home, plan.cwd, SYSTEM_APPEND, plan.tools,
                                self.log, max_turns=int(self.settings["max_turns_per_run"]) or None)
        result: ResultMessage | None = None
        async with ClaudeSDKClient(options=options) as client:
            await client.query(plan.prompt)
            async for msg in client.receive_response():
                log_message(self.log, msg)
                if isinstance(msg, ResultMessage):
                    result = msg
        return self._outcome(result)

    # -- planning ------------------------------------------------------------------

    async def _workspace(self, branch: str, base: str, pr_number: int | None = None) -> Workspace:
        item = self.ctx.item
        ws = Workspace(self.ctx.home, item["repo"], item["kind"], item["number"], self.ctx.gh.token)
        self.log("info", f"Preparing workspace on branch {branch}")
        await ws.prepare(branch, base, self.settings["git_author_name"], self.settings["git_author_email"],
                         pr_number=pr_number)
        return ws

    def _toolkit(self, ws: Workspace, base: str, protected: set[str]) -> ToolKit:
        return ToolKit(gh=self.ctx.gh, repo=self.ctx.item["repo"], signature=self.settings["signature"],
                       workspace=ws, protected=protected, base_branch=base, log=self.log)

    async def _plan_issue(self) -> _Plan:
        gh, item, pending = self.ctx.gh, self.ctx.item, self.ctx.work.pending
        repo, number = item["repo"], item["number"]
        default_branch = (await gh.repo(repo))["default_branch"]
        issue = await gh.issue(repo, number)
        comments = await gh.issue_comments(repo, number)
        branch = f"bugsbot/issue-{number}"
        ws = await self._workspace(branch, default_branch)

        kit = self._toolkit(ws, default_branch, {default_branch, "main", "master"})
        labels = [label for label in item["labels"] if label in self.ctx.tracked_labels]
        tools = [kit.comment(number), kit.reply_to_comment(number, pending, self.replied), kit.push_branch(),
                 kit.open_pull_request(labels=labels, resolves=number, on_opened=self._record_pr)]
        prompt = issue_prompt(repo, issue, comments, pending, branch, default_branch, item["linked_numbers"])
        return _Plan(ws.path, prompt, tools)

    async def _plan_pr(self) -> _Plan:
        gh, item, pending = self.ctx.gh, self.ctx.item, self.ctx.work.pending
        repo, number = item["repo"], item["number"]
        default_branch = (await gh.repo(repo))["default_branch"]
        pr = await gh.pr_context(repo, number)
        base = pr["baseRefName"]
        ws = await self._workspace(pr["headRefName"], base, pr_number=number)

        kit = self._toolkit(ws, base, {default_branch, base, "main", "master"})
        tools = [kit.comment(number), kit.reply_to_comment(number, pending, self.replied), kit.push_branch()]
        return _Plan(ws.path, pr_prompt(repo, pr, pending), tools)

    def _record_pr(self, pr: dict[str, Any], created: bool, labels: list[str]) -> None:
        """Track a PR the agent opened so the harness watches it and links it to the issue."""
        store, repo = self.ctx.store, self.ctx.item["repo"]
        store.upsert_item(repo, pr["number"], "pr", pr["title"], pr["html_url"], labels, "")
        pr_item = store.item_by_number(repo, pr["number"])
        if pr_item and created:
            # A fresh PR has nothing to respond to until humans comment.
            store.update_item(pr_item["id"], status="idle", status_detail="opened by bugsbot")
        store.link_items(repo, self.ctx.item["number"], pr["number"])
        self.opened_prs.append(pr["number"])

    # -- outcome -------------------------------------------------------------------

    def _outcome(self, result: ResultMessage | None) -> RunResult:
        missing = sorted({p.key for p in self.ctx.work.pending} - self.replied)
        notes = [(result.result or "").strip() if result else ""]
        if self.opened_prs:
            notes.append(f"PRs: {', '.join(f'#{n}' for n in self.opened_prs)}")
        if missing:
            notes.append(f"No reply posted for: {', '.join(missing)}")
        summary = "\n".join(n for n in notes if n)

        if result is None:
            return RunResult("failed", summary or "agent produced no result")
        if result.is_error:
            summary = f"{result.subtype}: {'; '.join(result.errors or [])}\n{summary}"
            if needs_login(summary):
                summary = f"{LOGIN_HINT}\n{summary}"
        # On a PR, every pending comment must be answered for the run to count as done.
        failed = result.is_error or (missing and self.ctx.item["kind"] == "pr")
        return RunResult("failed" if failed else "succeeded", summary.strip(),
                         result.num_turns, result.total_cost_usd)
