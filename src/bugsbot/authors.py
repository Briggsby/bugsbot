"""Whose issues, PRs and comments bugsbot acts on.

A repo with trusted authors configured only accepts those users, members of those orgs, and
bugsbot's own GitHub account. A repo without them falls back to the global setting: anyone,
or only owners/members/collaborators. Bot accounts are ignored unless explicitly trusted.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from bugsbot.github import TRUSTED_ASSOCIATIONS, GitHub, GitHubError

# (login, author type e.g. "User"/"Bot", author_association) -> trusted?
AuthorCheck = Callable[[str, str | None, str | None], bool]

MEMBERSHIP_TTL = 600


def _is_bot(login: str, author_type: str | None) -> bool:
    return author_type == "Bot" or login.endswith("[bot]")


def association_policy(trusted_only: bool) -> AuthorCheck:
    """The fallback when a repo has no trusted authors configured."""
    def allow(login: str, author_type: str | None, association: str | None) -> bool:
        if _is_bot(login, author_type):
            return False
        return not trusted_only or (association or "") in TRUSTED_ASSOCIATIONS
    return allow


class MembershipCache:
    """Org membership lookups, cached across polls."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[bool, float]] = {}

    def get(self, org: str, login: str) -> bool | None:
        hit = self._cache.get((org, login))
        return hit[0] if hit and time.time() - hit[1] < MEMBERSHIP_TTL else None

    async def resolve(self, gh: GitHub, orgs: Iterable[str], logins: Iterable[str]) -> None:
        for org in orgs:
            for login in logins:
                if self.get(org, login) is None:
                    try:
                        member = await gh.is_org_member(org, login)
                    except GitHubError:
                        member = False
                    self._cache[(org, login)] = (member, time.time())


class TrustedAuthors:
    """An explicit per-repo allowlist of users and orgs."""

    def __init__(self, users: list[str], orgs: list[str], bot_login: str | None, memberships: MembershipCache):
        self.users = {u.lower() for u in users}
        self.orgs = [o.lower() for o in orgs]
        self.bot_login = (bot_login or "").lower()
        self.memberships = memberships

    async def prepare(self, gh: GitHub, logins: Iterable[str]) -> AuthorCheck:
        """Resolve org membership for `logins`, then return a synchronous check."""
        pending = {l.lower() for l in logins if l} - self.users - {self.bot_login}
        await self.memberships.resolve(gh, self.orgs, pending)

        def allow(login: str, author_type: str | None, association: str | None) -> bool:
            login = login.lower()
            if login in self.users or (login == self.bot_login and login):
                return True
            if _is_bot(login, author_type):
                return False
            return any(self.memberships.get(org, login) for org in self.orgs)
        return allow


def issue_logins(issue: dict[str, Any], comments: list[dict[str, Any]]) -> set[str]:
    return {issue["user"]["login"], *(c["user"]["login"] for c in comments if c.get("user"))}


def pr_logins(pr: dict[str, Any]) -> set[str]:
    nodes = [pr, *pr["comments"]["nodes"], *pr["reviews"]["nodes"],
             *(c for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"])]
    return {n["author"]["login"] for n in nodes if n.get("author")}
