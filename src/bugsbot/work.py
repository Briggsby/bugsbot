"""Pure functions that decide what (if anything) an agent needs to do for an item."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bugsbot.github import TRUSTED_ASSOCIATIONS, is_bugsbot, replied_keys


@dataclass
class PendingComment:
    key: str             # stable id the agent uses to reply: issue:<id> | review:<id> | thread:<id>
    kind: str            # 'issue' | 'review' | 'thread'
    author: str
    body: str
    url: str
    created_at: str
    reply_target: int    # REST id to reply to (thread root for review threads)
    path: str | None = None
    line: int | None = None
    diff_hunk: str | None = None


@dataclass
class Work:
    trigger: str                       # human-readable reason
    trigger_key: str                   # changes whenever there is new work
    pending: list[PendingComment] = field(default_factory=list)


def _human(author_login: str | None, author_type: str | None, association: str | None,
           body: str | None, trusted_only: bool) -> bool:
    if is_bugsbot(body):
        return False
    if not author_login or author_type == "Bot" or author_login.endswith("[bot]"):
        return False
    return not trusted_only or (association or "") in TRUSTED_ASSOCIATIONS


def issue_work(comments: list[dict[str, Any]], trusted_only: bool) -> Work | None:
    """An issue needs work if bugsbot has never commented, or humans have commented since."""
    bot_indexes = [i for i, c in enumerate(comments) if is_bugsbot(c.get("body"))]
    if not bot_indexes:
        return Work(trigger="new issue", trigger_key="initial")
    after = comments[bot_indexes[-1] + 1:]
    pending = [
        PendingComment(
            key=f"issue:{c['id']}", kind="issue", author=c["user"]["login"], body=c.get("body") or "",
            url=c["html_url"], created_at=c["created_at"], reply_target=c["id"],
        )
        for c in after
        if _human(c["user"]["login"], c["user"].get("type"), c.get("author_association"),
                  c.get("body"), trusted_only)
    ]
    if not pending:
        return None
    return Work(trigger=f"{len(pending)} new comment(s) on issue",
                trigger_key=",".join(p.key for p in pending), pending=pending)


def pr_work(pr: dict[str, Any], trusted_only: bool) -> Work | None:
    """Collect every comment on a PR that bugsbot has not yet replied to directly."""
    def login(node: dict[str, Any]) -> tuple[str | None, str | None]:
        author = node.get("author") or {}
        return author.get("login"), author.get("__typename")

    comments = pr["comments"]["nodes"]
    reviews = pr["reviews"]["nodes"]
    threads = pr["reviewThreads"]["nodes"]

    replied: set[str] = set()
    for node in comments + [c for t in threads for c in t["comments"]["nodes"]]:
        if is_bugsbot(node.get("body")):
            replied |= replied_keys(node["body"])

    pending: list[PendingComment] = []
    for c in comments:
        key = f"issue:{c['databaseId']}"
        who, typ = login(c)
        if key not in replied and _human(who, typ, c.get("authorAssociation"), c.get("body"), trusted_only):
            pending.append(PendingComment(key=key, kind="issue", author=who or "", body=c["body"],
                                          url=c["url"], created_at=c["createdAt"],
                                          reply_target=c["databaseId"]))
    for r in reviews:
        key = f"review:{r['databaseId']}"
        who, typ = login(r)
        if (r.get("body") or "").strip() and key not in replied and _human(
                who, typ, r.get("authorAssociation"), r.get("body"), trusted_only):
            pending.append(PendingComment(key=key, kind="review", author=who or "",
                                          body=f"[review: {r['state']}]\n{r['body']}", url=r["url"],
                                          created_at=r["createdAt"], reply_target=r["databaseId"]))
    for t in threads:
        nodes = t["comments"]["nodes"]
        if t["isResolved"] or not nodes:
            continue
        root = nodes[0]["databaseId"]
        last_bot = max((i for i, c in enumerate(nodes) if is_bugsbot(c.get("body"))), default=-1)
        for c in nodes[last_bot + 1:]:
            who, typ = login(c)
            if _human(who, typ, c.get("authorAssociation"), c.get("body"), trusted_only):
                pending.append(PendingComment(
                    key=f"thread:{c['databaseId']}", kind="thread", author=who or "", body=c["body"],
                    url=c["url"], created_at=c["createdAt"], reply_target=root,
                    path=t.get("path"), line=t.get("line"), diff_hunk=c.get("diffHunk"),
                ))
    if not pending:
        return None
    pending.sort(key=lambda p: p.created_at)
    return Work(trigger=f"{len(pending)} unanswered comment(s) on PR",
                trigger_key=",".join(sorted(p.key for p in pending)), pending=pending)
