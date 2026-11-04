"""GitHub OAuth sign-in for the web UI.

Uses a GitHub OAuth App (client ID/secret from the bugsbot settings). Who may sign in is
decided by an allowlist; with nothing configured, nobody can sign in via GitHub.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from bugsbot.github import API, GitHub, GitHubError, resolve_token
from bugsbot.store import Store

log = logging.getLogger("bugsbot.oauth")

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
STATE_COOKIE = "bugsbot_oauth_state"
GITHUB_SESSION_TTL = 24 * 3600  # short, so allowlist removals take effect within a day
WRITE_PERMISSIONS = {"admin", "maintain", "write"}


def _csv(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


class OAuthError(RuntimeError):
    pass


class GitHubOAuth:
    def __init__(self, store: Store):
        self.store = store

    @property
    def configured(self) -> bool:
        s = self.store.settings()
        return bool(s["github_oauth_client_id"] and s["github_oauth_client_secret"])

    def authorize_url(self, redirect_uri: str) -> tuple[str, str]:
        """Returns (url to send the browser to, state value to bind to the browser)."""
        s = self.store.settings()
        state = secrets.token_urlsafe(24)
        scope = "read:org" if _csv(s["oauth_allowed_orgs"]) else ""
        query = {"client_id": s["github_oauth_client_id"], "redirect_uri": redirect_uri,
                 "state": state, "scope": scope, "allow_signup": "false"}
        return f"{AUTHORIZE_URL}?{urlencode(query)}", state

    async def complete(self, code: str, redirect_uri: str) -> str:
        """Exchange the code, identify the user, and check the allowlist. Returns the login."""
        s = self.store.settings()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TOKEN_URL, headers={"Accept": "application/json"}, data={
                "client_id": s["github_oauth_client_id"],
                "client_secret": s["github_oauth_client_secret"],
                "code": code, "redirect_uri": redirect_uri,
            })
            data = resp.json() if resp.content else {}
            token = data.get("access_token")
            if not token:
                raise OAuthError(f"GitHub did not issue a token: {data.get('error_description') or resp.status_code}")
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                       "User-Agent": "bugsbot"}
            user = (await client.get(f"{API}/user", headers=headers)).json()
            login = user.get("login")
            if not login:
                raise OAuthError("could not read your GitHub identity")
            if await self._allowed(client, headers, login, s):
                log.info("GitHub sign-in: %s", login)
                return login
        log.warning("GitHub sign-in refused for %s", login)
        raise OAuthError(f"@{login} is not allowed to use this bugsbot instance")

    async def _allowed(self, client: httpx.AsyncClient, headers: dict[str, str], login: str,
                       s: dict[str, Any]) -> bool:
        if login.lower() in _csv(s["oauth_allowed_users"]):
            return True
        for org in _csv(s["oauth_allowed_orgs"]):
            # Uses the user's own token (read:org) so private memberships count too.
            r = await client.get(f"{API}/user/memberships/orgs/{org}", headers=headers)
            if r.status_code == 200 and r.json().get("state") == "active":
                return True
        if s["oauth_allow_repo_collaborators"]:
            # Checked with the harness's token, which can see collaborator permissions.
            try:
                gh = GitHub(resolve_token(s["github_token"]))
            except GitHubError:
                return False
            try:
                for repo in self.store.repos():
                    try:
                        perm = await gh.collaborator_permission(repo, login)
                    except GitHubError:
                        continue
                    if perm in WRITE_PERMISSIONS:
                        return True
            finally:
                await gh.close()
        return False
