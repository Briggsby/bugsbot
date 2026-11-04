"""Minimal async GitHub REST/GraphQL client.

Deliberately has no method that merges a pull request: the harness never merges.
Every piece of text bugsbot publishes goes through `sign()` so it carries a visible
signature plus a hidden marker the harness uses to recognise its own comments.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

import httpx

from .store import DEFAULT_SIGNATURE

API = "https://api.github.com"
MARKER = "<!-- bugsbot -->"
REPLY_MARKER_RE = re.compile(r"<!-- bugsbot:reply-to:([a-z]+:\d+) -->")
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def sign(body: str, signature: str, reply_to: list[str] | None = None) -> str:
    """Append the visible signature and hidden markers to text bugsbot publishes."""
    signature = signature.strip() or DEFAULT_SIGNATURE
    markers = [MARKER] + [f"<!-- bugsbot:reply-to:{key} -->" for key in reply_to or []]
    return f"{body.rstrip()}\n\n---\n{signature}\n" + "\n".join(markers)


def is_bugsbot(body: str | None) -> bool:
    return bool(body) and MARKER in body  # type: ignore[operator]


def replied_keys(body: str | None) -> set[str]:
    return set(REPLY_MARKER_RE.findall(body or ""))


def resolve_token(configured: str) -> str:
    """Use the configured token, falling back to the GitHub CLI's login."""
    if configured.strip():
        return configured.strip()
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return ""


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str):
        if not token:
            raise GitHubError("No GitHub token: set one in Settings or run `gh auth login`.")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=API,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bugsbot",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._client.request(method, path, **kw)
        if resp.status_code >= 400:
            raise GitHubError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else None

    async def _paginate(self, path: str, params: dict[str, Any] | None = None,
                        max_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params = {"per_page": 100, **(params or {})}
        url: str | None = path
        for _ in range(max_pages):
            if url is None:
                break
            resp = await self._client.get(url, params=params if url == path else None)
            if resp.status_code >= 400:
                raise GitHubError(f"GET {url} -> {resp.status_code}: {resp.text[:500]}")
            out.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
        return out

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        data = await self._req("POST", "/graphql", json={"query": query, "variables": variables})
        if data.get("errors"):
            raise GitHubError(f"GraphQL error: {data['errors']}")
        return data["data"]

    # -- reads ----------------------------------------------------------------

    async def viewer(self) -> dict[str, Any]:
        return await self._req("GET", "/user")

    async def user_repos(self) -> list[dict[str, Any]]:
        repos = await self._paginate("/user/repos", {"sort": "updated"}, max_pages=5)
        return [{"full_name": r["full_name"], "private": r["private"],
                 "description": r.get("description") or ""} for r in repos]

    async def repo(self, full_name: str) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{full_name}")

    async def labelled_open(self, full_name: str, label: str) -> list[dict[str, Any]]:
        """Open issues *and* PRs carrying `label` (the issues API returns both)."""
        return await self._paginate(f"/repos/{full_name}/issues",
                                    {"labels": label, "state": "open"}, max_pages=5)

    async def issue(self, full_name: str, number: int) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{full_name}/issues/{number}")

    async def issue_comments(self, full_name: str, number: int) -> list[dict[str, Any]]:
        return await self._paginate(f"/repos/{full_name}/issues/{number}/comments")

    async def pr_context(self, full_name: str, number: int) -> dict[str, Any]:
        owner, name = full_name.split("/", 1)
        data = await self.graphql(PR_QUERY, {"owner": owner, "name": name, "number": number})
        return data["repository"]["pullRequest"]

    async def collaborator_permission(self, full_name: str, login: str) -> str:
        data = await self._req("GET", f"/repos/{full_name}/collaborators/{login}/permission")
        return data.get("permission", "none")

    # -- writes (all text is signed by the caller via `sign`) ------------------

    async def create_comment(self, full_name: str, number: int, body: str) -> dict[str, Any]:
        return await self._req("POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body})

    async def reply_review_comment(self, full_name: str, pr: int, comment_id: int, body: str) -> dict[str, Any]:
        return await self._req("POST", f"/repos/{full_name}/pulls/{pr}/comments/{comment_id}/replies",
                               json={"body": body})

    async def create_pr(self, full_name: str, head: str, base: str, title: str, body: str,
                        draft: bool) -> dict[str, Any]:
        return await self._req("POST", f"/repos/{full_name}/pulls", json={
            "head": head, "base": base, "title": title, "body": body, "draft": draft,
        })

    async def find_open_pr(self, full_name: str, head_branch: str) -> dict[str, Any] | None:
        owner = full_name.split("/", 1)[0]
        prs = await self._req("GET", f"/repos/{full_name}/pulls",
                              params={"head": f"{owner}:{head_branch}", "state": "open"})
        return prs[0] if prs else None

    async def add_labels(self, full_name: str, number: int, labels: list[str]) -> None:
        if labels:
            await self._req("POST", f"/repos/{full_name}/issues/{number}/labels", json={"labels": labels})


PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title body url state isCrossRepository
      headRefName baseRefName
      author { login }
      comments(last: 100) {
        nodes { databaseId body createdAt url authorAssociation author { login __typename } }
      }
      reviews(last: 100) {
        nodes { databaseId body state createdAt url authorAssociation author { login __typename } }
      }
      reviewThreads(last: 100) {
        nodes {
          isResolved isOutdated path line
          comments(first: 100) {
            nodes {
              databaseId body createdAt url diffHunk authorAssociation
              author { login __typename }
              replyTo { databaseId }
            }
          }
        }
      }
    }
  }
}
"""
