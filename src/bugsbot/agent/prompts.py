"""Task prompts for issue and PR runs."""

from __future__ import annotations

from typing import Any

from bugsbot.work import PendingComment


def _fmt_pending(p: PendingComment) -> str:
    where = f" on `{p.path}`" + (f" line {p.line}" if p.line else "") if p.path else ""
    hunk = f"\nDiff context:\n```diff\n{p.diff_hunk[-1500:]}\n```" if p.diff_hunk else ""
    return f"### [{p.key}] {p.kind} comment by @{p.author}{where} ({p.created_at})\n{p.url}\n{hunk}\n\n{p.body}\n"


def _fmt_comment(c: dict[str, Any]) -> str:
    return f"--- @{c['user']['login']} ({c['created_at']}):\n{c.get('body') or ''}\n"


def issue_prompt(repo: str, issue: dict[str, Any], comments: list[dict[str, Any]],
                 pending: list[PendingComment], branch: str, default_branch: str,
                 linked_prs: list[int]) -> str:
    number = issue["number"]
    linked = ", ".join(f"#{n}" for n in linked_prs) or "none"
    history = "\n".join(_fmt_comment(c) for c in comments) or "(no comments)"
    new_comments = "\n".join(_fmt_pending(p) for p in pending)
    return f"""You are handling GitHub issue {repo}#{number}: {issue['title']}
{issue['html_url']}

Your working directory is a fresh checkout of {repo} on branch `{branch}` (based on `{default_branch}`).
Pull requests previously opened by bugsbot for this issue: {linked}

## Issue body (by @{issue['user']['login']})
{issue.get('body') or '(empty)'}

## Comment history
{history}

{"## New comments needing a response" + chr(10) + new_comments if new_comments else ""}

## What to do
1. Understand the issue and investigate the codebase.
2. If the issue is unclear, too large, or not something code changes should solve, post a
   `comment` explaining what you need or why, and stop.
3. Otherwise, post a short `comment` saying you are working on it and outlining your plan.
4. Implement the fix on `{branch}`, add or update tests, run them, and commit with clear messages.
   If the work should be split into several PRs, create additional branches (`git checkout -b
   bugsbot/issue-{number}-<topic>`) and open one PR per branch.
5. Call `open_pull_request` for each branch. Write a description covering what changed,
   why, and how it was tested. If a bugsbot PR already exists for this branch, pushing updates it.
6. Finish with a `comment` on the issue summarising the outcome and linking the PR(s).
{"7. Reply to each of the new comments above with `reply_to_comment`." if new_comments else ""}
Never merge any PR.
"""


def pr_prompt(repo: str, pr: dict[str, Any], pending: list[PendingComment]) -> str:
    base = pr["baseRefName"]
    fork_note = ("\nNOTE: this PR comes from a fork, so you cannot push to it. Reply to comments "
                 "but explain that a maintainer must apply any changes.\n") if pr["isCrossRepository"] else ""
    comments = "\n".join(_fmt_pending(p) for p in pending)
    return f"""You are responding to review feedback on pull request {repo}#{pr['number']}: {pr['title']}
{pr['url']}

Your working directory is a checkout of the PR branch `{pr['headRefName']}` (base `{base}`).
Use `git log` / `git diff origin/{base}...HEAD` to see the PR's changes.{fork_note}

## PR description
{pr.get('body') or '(empty)'}

## Comments you have not yet responded to
{comments}

## What to do
1. Read ALL of the comments above before acting, and study the relevant code.
2. For EACH comment, decide whether it is correct and whether a change is justified. Reply to it
   with `reply_to_comment` giving your assessment: agree and say what you will change, or
   respectfully disagree and explain why (with evidence from the code). Questions get answers.
3. Make the justified changes, run tests, commit with clear messages, and `push_branch`.
4. Then reply again to each comment you acted on with `reply_to_comment`, saying exactly what
   you did (reference the commit SHA). Every comment key above must receive at least one reply.
Never merge the PR.
"""
