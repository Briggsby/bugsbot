"""Tools agents use for every write that leaves the machine.

Each tool signs the text it publishes, and pushes go through `Workspace.push`, which
refuses protected branches. Nothing here can merge a pull request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import tool

from bugsbot.agent.session import Log
from bugsbot.github import GitHub, GitHubError, sign
from bugsbot.work import PendingComment
from bugsbot.workspace import GitError, Workspace

OnPrOpened = Callable[[dict[str, Any], bool, list[str]], None]  # (pr, newly_created, labels)


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


@dataclass
class ToolKit:
    gh: GitHub
    repo: str
    signature: str
    workspace: Workspace
    protected: set[str]   # branches push_branch refuses
    base_branch: str      # target of new PRs
    log: Log

    def _ok(self, text: str) -> dict[str, Any]:
        self.log("tool_ok", text[:2000])
        return {"content": [{"type": "text", "text": text}]}

    def _err(self, text: str) -> dict[str, Any]:
        self.log("tool_error", text[:2000])
        return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}

    def comment(self, number: int | None = None):
        """Top-level comment on `number`, or on any issue/PR the agent names if None."""
        props = {"body": {"type": "string"}}
        if number is None:
            props["number"] = {"type": "integer"}
        target = f"#{number}" if number else "an issue or PR in this repository"

        @tool("comment", f"Post a new top-level comment on {target}. The bugsbot signature is appended.",
              _obj(props, list(props)))
        async def comment(args: dict[str, Any]) -> dict[str, Any]:
            try:
                c = await self.gh.create_comment(self.repo, number or int(args["number"]),
                                                 sign(args["body"], self.signature))
            except GitHubError as e:
                return self._err(str(e))
            return self._ok(f"Commented: {c['html_url']}")
        return comment

    def reply_to_comment(self, number: int, pending: list[PendingComment], replied: set[str]):
        """Direct replies to the given pending comments; adds each answered key to `replied`."""
        by_key = {p.key: p for p in pending}

        @tool("reply_to_comment",
              "Reply directly to one of the pending comments listed in your task, by its key "
              "(e.g. 'thread:123', 'issue:456', 'review:789'). Review-thread comments get an "
              "in-thread reply; others get a comment that quotes and links the original. "
              "The bugsbot signature is appended automatically.",
              _obj({"comment_key": {"type": "string"}, "body": {"type": "string"}}, ["comment_key", "body"]))
        async def reply_to_comment(args: dict[str, Any]) -> dict[str, Any]:
            p = by_key.get(args["comment_key"])
            if p is None:
                return self._err(f"Unknown comment key {args['comment_key']!r}. Valid keys: {sorted(by_key)}")
            body = args["body"]
            if p.kind != "thread":  # no threading outside review comments, so quote the original
                first = (p.body.strip().splitlines() or [""])[0][:200]
                body = f"> @{p.author}: {first}\n> ([original comment]({p.url}))\n\n{body}"
            body = sign(body, self.signature, reply_to=[p.key])
            try:
                if p.kind == "thread":
                    c = await self.gh.reply_review_comment(self.repo, number, p.reply_target, body)
                else:
                    c = await self.gh.create_comment(self.repo, number, body)
            except GitHubError as e:
                return self._err(str(e))
            replied.add(p.key)
            return self._ok(f"Replied to {p.key}: {c['html_url']}")
        return reply_to_comment

    def push_branch(self):
        @tool("push_branch", "Push the currently checked-out branch to GitHub (fast-forward only; "
              "never to the default branch). Commit your changes first.", _obj({}, []))
        async def push_branch(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return self._ok(await self.workspace.push(self.protected))
            except GitError as e:
                return self._err(str(e))
        return push_branch

    def open_pull_request(self, *, labels: list[str] | None, resolves: int | None = None,
                          on_opened: OnPrOpened | None = None):
        """Push and open (or find) a PR from the current branch.

        `labels` fixes the PR's labels; None lets the agent choose them. `resolves` adds a
        "Resolves #N" link unless the body already mentions #N.
        """
        props: dict[str, Any] = {
            "title": {"type": "string"},
            "body": {"type": "string", "description": "PR description in Markdown"},
            "draft": {"type": "boolean", "default": False},
        }
        if labels is None:
            props["labels"] = {"type": "array", "items": {"type": "string"}, "default": []}
        label_note = f"It is labelled {labels}. " if labels else ""

        @tool("open_pull_request",
              f"Push the current branch and open a pull request from it against {self.base_branch}. "
              f"{label_note}The bugsbot signature is appended. If a PR from this branch is already "
              "open, it is returned instead.", _obj(props, ["title", "body"]))
        async def open_pull_request(args: dict[str, Any]) -> dict[str, Any]:
            pr_labels = labels if labels is not None else list(args.get("labels") or [])
            try:
                pushed = await self.workspace.push(self.protected)
                branch = await self.workspace.current_branch()
                pr = await self.gh.find_open_pr(self.repo, branch)
                created = pr is None
                if pr is None:
                    body = args["body"]
                    if resolves and f"#{resolves}" not in body:
                        body += f"\n\nResolves #{resolves}"
                    pr = await self.gh.create_pr(self.repo, branch, self.base_branch, args["title"],
                                                 sign(body, self.signature), bool(args.get("draft")))
                await self.gh.add_labels(self.repo, pr["number"], pr_labels)
            except (GitError, GitHubError) as e:
                return self._err(str(e))
            if on_opened:
                on_opened(pr, created, pr_labels)
            verb = "Opened" if created else "Updated existing"
            return self._ok(f"{verb} PR #{pr['number']}: {pr['html_url']} (labels: {pr_labels})\n{pushed}")
        return open_pull_request
