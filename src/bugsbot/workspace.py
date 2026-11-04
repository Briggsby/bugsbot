"""Git checkouts the agents work in.

The harness owns all authenticated git traffic (clone/fetch/push). The agent's own
shell gets an environment with no usable GitHub credentials, so the only way its
work leaves the machine is through the harness's push tool, which refuses to push
to the default branch.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path


class GitError(RuntimeError):
    pass


def _auth_args(token: str) -> list[str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


async def git(cwd: Path, *args: str, token: str | None = None, check: bool = True) -> str:
    cmd = ["git", *(_auth_args(token) if token else []), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace").strip()
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}):\n{text}")
    return text


def agent_env(home: Path, author_name: str, author_email: str) -> dict[str, str]:
    """Environment for the agent's subprocesses: git works locally, GitHub auth does not."""
    gitconfig = home / "agent.gitconfig"
    gitconfig.write_text(
        f"[user]\n\tname = {author_name}\n\temail = {author_email}\n"
        "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"
    )
    gh_dir = home / "agent-gh-config"
    gh_dir.mkdir(exist_ok=True)
    return {
        "GIT_CONFIG_NOSYSTEM": "1",          # no system credential helpers (e.g. osxkeychain)
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "false",          # no pushing via the user's SSH keys
        "GH_CONFIG_DIR": str(gh_dir),        # gh CLI is unauthenticated for the agent
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
    }


class Workspace:
    def __init__(self, home: Path, repo: str, kind: str, number: int, token: str,
                 root: Path | None = None):
        self.repo = repo
        self.token = token
        self.path = (root or home / "workspaces") / repo.replace("/", "__") / f"{kind}-{number}"

    @classmethod
    def at(cls, path: Path, repo: str, token: str) -> "Workspace":
        """A Workspace for an existing checkout at `path`."""
        ws = cls.__new__(cls)
        ws.repo, ws.token, ws.path = repo, token, path
        return ws

    async def prepare(self, branch: str, base_branch: str, author_name: str, author_email: str,
                      pr_number: int | None = None) -> None:
        """Clone (or refresh) and check out `branch`, creating it from `base_branch` if new."""
        if not (self.path / ".git").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            await git(self.path.parent, "clone", f"https://github.com/{self.repo}.git",
                      self.path.name, token=self.token)
        await git(self.path, "fetch", "origin", "--prune", token=self.token)
        await git(self.path, "config", "user.name", author_name)
        await git(self.path, "config", "user.email", author_email)
        await git(self.path, "reset", "--hard", check=False)
        await git(self.path, "clean", "-fd", check=False)

        if pr_number is not None:
            # Works for same-repo and fork PRs alike.
            await git(self.path, "fetch", "origin", f"pull/{pr_number}/head", token=self.token)
            remote_head = await git(self.path, "ls-remote", "--heads", "origin", branch, token=self.token)
            start = f"origin/{branch}" if remote_head else "FETCH_HEAD"
        else:
            remote_head = await git(self.path, "ls-remote", "--heads", "origin", branch, token=self.token)
            start = f"origin/{branch}" if remote_head else f"origin/{base_branch}"
        await git(self.path, "checkout", "-B", branch, start)
        if remote_head:
            await git(self.path, "branch", f"--set-upstream-to=origin/{branch}", branch, check=False)

    async def current_branch(self) -> str:
        return await git(self.path, "rev-parse", "--abbrev-ref", "HEAD")

    async def push(self, protected: set[str]) -> str:
        """Push the current branch to the same-named remote branch. Never force, never to `protected`."""
        branch = await self.current_branch()
        if branch == "HEAD" or not branch:
            raise GitError("Not on a branch; check out a branch before pushing.")
        if branch in protected:
            raise GitError(f"Refusing to push to protected branch '{branch}'. Work on a feature branch.")
        status = await git(self.path, "status", "--porcelain")
        out = await git(self.path, "push", "origin", f"HEAD:refs/heads/{branch}", token=self.token)
        await git(self.path, "fetch", "origin", branch, token=self.token, check=False)
        await git(self.path, "branch", f"--set-upstream-to=origin/{branch}", branch, check=False)
        sha = await git(self.path, "rev-parse", "--short", "HEAD")
        note = "\nWarning: there are uncommitted changes that were NOT pushed." if status else ""
        return f"Pushed {branch} at {sha}.{note}\n{out}"
