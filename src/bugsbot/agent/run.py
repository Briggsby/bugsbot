"""One autonomous agent run for one issue or PR, on the configured provider."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bugsbot import providers
from bugsbot.agent.policy import SYSTEM_APPEND
from bugsbot.agent.prompts import issue_prompt, pr_prompt
from bugsbot.agent.toolspec import ToolSpec, build_tools, opened_prs, replied_keys
from bugsbot.authors import AuthorCheck
from bugsbot.github import GitHub, is_bugsbot
from bugsbot.providers.base import AgentJob, AgentOutcome, Provider
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
    author_allowed: AuthorCheck = lambda login, author_type, association: True  # noqa: E731


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
    tool_spec: ToolSpec


class AgentRun:
    def __init__(self, ctx: RunContext, provider: Provider | None = None):
        self.ctx = ctx
        self.settings = ctx.store.settings()
        self.provider = provider or providers.get(self.settings["agent_provider"])

    def log(self, type_: str, text: str) -> None:
        self.ctx.store.add_event(self.ctx.run_id, type_, text)

    async def run(self) -> RunResult:
        item = self.ctx.item
        plan = await (self._plan_issue() if item["kind"] == "issue" else self._plan_pr())
        self.log("prompt", plan.prompt)
        self.log("info", f"Running on {self.provider.label}")
        job = AgentJob(
            store=self.ctx.store, home=self.ctx.home, run_id=self.ctx.run_id, cwd=plan.cwd, prompt=plan.prompt,
            rules=SYSTEM_APPEND, log=self.log, tool_spec=plan.tool_spec,
            tools=build_tools(plan.tool_spec, self.ctx.gh, self.ctx.store, self.ctx.run_id,
                              self.settings["signature"]),
        )
        minutes = int(self.settings["max_minutes_per_run"])
        try:
            outcome = await asyncio.wait_for(self.provider.agent(job).run(), timeout=minutes * 60 or None)
        except asyncio.TimeoutError:
            outcome = AgentOutcome(ok=False, error=f"timed out after {minutes} minutes (Max minutes per run)")
        return self._outcome(outcome)

    # -- planning ------------------------------------------------------------------

    async def _workspace(self, branch: str, base: str, pr_number: int | None = None) -> Workspace:
        item = self.ctx.item
        ws = Workspace(self.ctx.home, item["repo"], item["kind"], item["number"], self.ctx.gh.token)
        self.log("info", f"Preparing workspace on branch {branch}")
        await ws.prepare(branch, base, self.settings["git_author_name"], self.settings["git_author_email"],
                         pr_number=pr_number)
        return ws

    async def _plan_issue(self) -> _Plan:
        gh, item, pending = self.ctx.gh, self.ctx.item, self.ctx.work.pending
        repo, number = item["repo"], item["number"]
        default_branch = (await gh.repo(repo))["default_branch"]
        issue = await gh.issue(repo, number)
        # Only show the agent comments from authors this repo trusts (plus bugsbot's own).
        comments = [c for c in await gh.issue_comments(repo, number)
                    if is_bugsbot(c.get("body")) or self.ctx.author_allowed(
                        c["user"]["login"], c["user"].get("type"), c.get("author_association"))]
        branch = f"bugsbot/issue-{number}"
        ws = await self._workspace(branch, default_branch)
        spec = ToolSpec(kind="issue", repo=repo, cwd=str(ws.path), base_branch=default_branch,
                        protected=sorted({default_branch, "main", "master"}), number=number, pending=pending,
                        pr_labels=[label for label in item["labels"] if label in self.ctx.tracked_labels])
        prompt = issue_prompt(repo, issue, comments, pending, branch, default_branch, item["linked_numbers"])
        return _Plan(ws.path, prompt, spec)

    async def _plan_pr(self) -> _Plan:
        gh, item, pending = self.ctx.gh, self.ctx.item, self.ctx.work.pending
        repo, number = item["repo"], item["number"]
        default_branch = (await gh.repo(repo))["default_branch"]
        pr = await gh.pr_context(repo, number)
        base = pr["baseRefName"]
        ws = await self._workspace(pr["headRefName"], base, pr_number=number)
        spec = ToolSpec(kind="pr", repo=repo, cwd=str(ws.path), base_branch=base,
                        protected=sorted({default_branch, base, "main", "master"}), number=number, pending=pending)
        return _Plan(ws.path, pr_prompt(repo, pr, pending), spec)

    # -- outcome -------------------------------------------------------------------

    def _outcome(self, outcome: AgentOutcome) -> RunResult:
        store, run_id = self.ctx.store, self.ctx.run_id
        missing = sorted({p.key for p in self.ctx.work.pending} - replied_keys(store, run_id))
        notes = [outcome.result]
        if prs := opened_prs(store, run_id):
            notes.append(f"PRs: {', '.join(f'#{n}' for n in prs)}")
        if missing:
            notes.append(f"No reply posted for: {', '.join(missing)}")
        summary = "\n".join(n for n in notes if n)
        if not outcome.ok:
            summary = f"{outcome.error}\n{summary}".strip()
            if hint := self.provider.login_hint(summary):
                summary = f"{hint}\n{summary}"
        # On a PR, every pending comment must be answered for the run to count as done.
        failed = not outcome.ok or bool(missing and self.ctx.item["kind"] == "pr")
        return RunResult("failed" if failed else "succeeded", summary.strip(), outcome.num_turns, outcome.cost_usd)
