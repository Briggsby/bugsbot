"""Ad-hoc Claude Code Remote Control workers.

Each worker is a `claude remote-control` process on this host, working in its own
checkout. You chat with it from the Claude app (claude.ai/code or mobile), signed in
as the Claude account logged in on this host. bugsbot starts and stops it, installs
the same rules and signed tools as its other agents, and records it as a run of kind
'worker' so its duration shows next to the issue/PR runs.

bugsbot starts and stops workers but doesn't control them. Once a worker's session is
connected, the Claude account drives it from the Claude app: bugsbot sends it no input and
stops recording its terminal, so the conversation never reaches bugsbot's UI. A worker ends
when its session ends in the app, when someone stops it from bugsbot, when it fails to
connect within STARTUP_TIMEOUT, or when bugsbot shuts down.

Remote Control needs a real terminal, so the process runs on a pseudo-terminal. Its
start-up output is logged (ANSI stripped) so start-up problems are visible in the UI.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import termios
import time
import traceback
from pathlib import Path

from bugsbot.agent import guardrails
from bugsbot.anthropic_check import needs_login
from bugsbot.github import GitHub, resolve_token
from bugsbot.store import Store
from bugsbot.workspace import Workspace, agent_env, git

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]")
LINK_RE = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_/?=&.-]+")
TRUST_RE = re.compile(r"trust", re.I)
TRUST_CONTEXT_RE = re.compile(r"folder|files|directory|project", re.I)
MAX_LOG_LINES = 400
STARTUP_TIMEOUT = 180  # seconds for a worker's session to connect

WORKER_RULES = """
You are a Remote Control worker started from the bugsbot web UI. A maintainer chats with
you from the Claude app. Do what they ask and ask when something is ambiguous. To push,
open PRs or comment on GitHub, use the bugsbot MCP tools (git push and gh writes are blocked).
"""


def claude_sessions_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "sessions"


def find_session_link(cwd: Path, since: float) -> str | None:
    """Claude Code records running sessions in ~/.claude/sessions/<pid>.json; Remote Control
    sessions carry a bridgeSessionId. This is best-effort: the format is not a public API."""
    for path in claude_sessions_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (data.get("cwd") == str(cwd) and data.get("bridgeSessionId")
                and data.get("startedAt", 0) / 1000 >= since - 5):
            return f"https://claude.ai/code/{data['bridgeSessionId']}"
    return None


def workers_root(home: Path) -> Path:
    """Every worker checkout lives under this one folder, so trusting it once (in Claude Code)
    covers all future workers."""
    return home / "workers"


def claude_config_file() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"


def trust_status(home: Path) -> dict[str, object]:
    """Whether Claude Code has trusted the workers folder (or a parent of it).

    Read-only: bugsbot never edits Claude Code's config. Trust is granted by running
    `claude` in the folder once and accepting the prompt.
    """
    root = workers_root(home)
    root.mkdir(parents=True, exist_ok=True)
    command = f"cd {shlex.quote(str(root))} && claude"
    try:
        projects = json.loads(claude_config_file().read_text()).get("projects", {})
    except (OSError, ValueError):
        projects = {}
    for folder in (root, *root.parents):
        if projects.get(str(folder), {}).get("hasTrustDialogAccepted"):
            return {"trusted": True, "root": str(root), "trusted_via": str(folder), "command": command}
    return {"trusted": False, "root": str(root), "command": command}


def worker_env(store: Store, home: Path) -> dict[str, str]:
    settings = store.settings()
    env = {**os.environ, **agent_env(home, settings["git_author_name"], settings["git_author_email"])}
    # Remote Control uses the host's claude.ai login; an API key would override it.
    for key in ("ANTHROPIC_API_KEY", "CLAUDECODE"):
        env.pop(key, None)
    env.setdefault("TERM", "xterm-256color")
    return env


async def claude_account(store: Store, home: Path) -> dict[str, object]:
    """Who Claude Code on this host is logged in as, from `claude auth status --json`."""
    claude = shutil.which("claude")
    if not claude:
        return {"ok": False, "detail": "the `claude` CLI isn't installed on this host"}
    try:
        proc = await asyncio.create_subprocess_exec(
            claude, "auth", "status", "--json", env=worker_env(store, home),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        status = json.loads(out)
    except (OSError, ValueError, asyncio.TimeoutError) as e:
        return {"ok": False, "detail": f"couldn't read Claude Code's login status: {e}"}
    info = {k: status.get(k) for k in ("loggedIn", "authMethod", "email", "orgName", "subscriptionType")}
    if not status.get("loggedIn"):
        return {**info, "ok": False, "detail": "not logged in"}
    if status.get("authMethod") != "claude.ai":
        return {**info, "ok": False,
                "detail": f"logged in via {status.get('authMethod')}, but Remote Control needs a Claude account"}
    return {**info, "ok": True, "detail": "ready"}


class RemoteWorker:
    def __init__(self, store: Store, home: Path, run_id: int, repo: str, branch: str, name: str,
                 label: str = ""):
        self.store, self.home, self.run_id = store, home, run_id
        self.repo, self.branch, self.name = repo, branch, name
        self.label = label  # the name the maintainer typed, if any
        self.default_branch = ""
        self.link: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self._seen: set[str] = set()
        self._startup_text = ""
        self._login_warned = False
        self.untrusted = False
        self._stop_reason = "stopped from bugsbot"
        self.task = asyncio.create_task(self._main())

    def log(self, type_: str, text: str) -> None:
        self.store.add_event(self.run_id, type_, text)

    def stop(self, reason: str = "stopped from bugsbot") -> None:
        self._stop_reason = reason
        self.task.cancel()

    async def _prepare_cwd(self) -> Path:
        settings = self.store.settings()
        if not self.repo:
            cwd = workers_root(self.home) / "scratch" / f"worker-{self.run_id}"
            cwd.mkdir(parents=True, exist_ok=True)
            if not (cwd / ".git").exists():
                await git(cwd, "init", "-q")
            return cwd
        gh = GitHub(resolve_token(settings["github_token"]))
        try:
            default_branch = (await gh.repo(self.repo))["default_branch"]
            if self.branch in (default_branch, "main", "master"):
                raise ValueError(f"'{self.branch}' is protected; name a feature branch or leave it blank")
            ws = Workspace(self.home, self.repo, "worker", self.run_id, gh.token, root=workers_root(self.home))
            # A named branch is checked out if it exists on GitHub, otherwise created from the
            # default branch. With no name the worker starts on the default branch and creates
            # its own branch once it knows what the task is (see branch_rules).
            branch = self.branch or default_branch
            self.log("info", f"Preparing checkout of {self.repo} on branch {branch}")
            await ws.prepare(branch, default_branch, settings["git_author_name"], settings["git_author_email"])
            self.default_branch = default_branch
            return ws.path
        finally:
            await gh.close()

    def branch_rules(self) -> str:
        if not self.repo:
            return ""
        if self.branch:
            return f"\nWork on the branch `{self.branch}`, which is already checked out.\n"
        hint = f" The worker was named \"{self.label}\", which may describe the task." if self.label else ""
        return (f"\nYou start on `{self.default_branch}`, which you can't push to. Before committing, create a "
                f"branch named for the task, e.g. `git checkout -b bugsbot/<short-kebab-case-topic>`, based on "
                f"what the maintainer asks.{hint} Tell them the branch name you chose.\n")

    def _on_output(self, master: int, data: bytes) -> None:
        if self.link:
            return  # connected: the session belongs to the Claude app now; output is drained, not kept
        text = ANSI_RE.sub("", data.decode(errors="replace"))
        # Prompts can arrive split across reads, so look at all start-up text so far.
        self._startup_text = (self._startup_text + text)[-4000:]
        if (not self.untrusted and TRUST_RE.search(self._startup_text)
                and TRUST_CONTEXT_RE.search(self._startup_text)):
            # bugsbot never answers security prompts itself; fail with instructions instead.
            self.untrusted = True
            status = trust_status(self.home)
            self.log("error", "Claude Code is asking whether to trust this worker's folder, so the worker "
                              f"can't start unattended. Trust the workers folder once: on this host run "
                              f"`{status['command']}`, accept the prompt, then /exit. Workers under "
                              f"{status['root']} are then trusted.")
        if not self.link and (m := LINK_RE.search(text)):
            self._set_link(m.group(0))
        if not self._login_warned and needs_login(text):
            self._login_warned = True
            self.log("error", "Claude Code on this host isn't logged in. Remote Control needs a Claude "
                              "subscription login (an API key won't work): as the OS user running bugsbot, "
                              "run `claude`, then /login with your Claude account.")
        for line in re.split(r"[\r\n]+", text):
            line = line.strip()
            if line and line not in self._seen and len(self._seen) < MAX_LOG_LINES:
                self._seen.add(line)
                self.log("terminal", line)

    def _set_link(self, link: str) -> None:
        self.link = link
        self.store.set_run_summary(self.run_id, link)
        self.log("tool_ok", f"Remote Control session ready: {link}")
        self.log("info", "Connected. From here on the worker is controlled from the Claude app; "
                         "bugsbot no longer records its activity (it can still stop it).")

    async def _main(self) -> None:
        status, error = "succeeded", ""
        master = -1
        try:
            claude = shutil.which("claude")
            if not claude:
                raise RuntimeError("the `claude` CLI is not installed on this host")
            cwd = await self._prepare_cwd()
            guardrails.install(cwd, self.home, self.run_id, self.repo or None, WORKER_RULES + self.branch_rules())

            master, slave = os.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
            cmd = [claude, "remote-control", "--spawn", "session", "--name", self.name,
                   "--remote-control-session-name-prefix", self.name, "--permission-mode", "bypassPermissions"]
            started = time.time()
            self.log("info", f"$ {' '.join(cmd[1:])}  (in {cwd})")
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=worker_env(self.store, self.home), stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            self.store.set_run_pid(self.run_id, self.proc.pid)

            loop = asyncio.get_running_loop()

            def readable() -> None:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    loop.remove_reader(master)
                    return
                if data:
                    self._on_output(master, data)
                else:
                    loop.remove_reader(master)
            loop.add_reader(master, readable)

            while self.proc.returncode is None:
                if not self.link and (link := find_session_link(cwd, started)):
                    self._set_link(link)
                if self.untrusted:
                    raise RuntimeError("the worker's folder isn't trusted by Claude Code (see above)")
                if not self.link and time.time() - started > STARTUP_TIMEOUT:
                    raise TimeoutError(f"the session didn't connect within {STARTUP_TIMEOUT}s "
                                       "(see the terminal output above)")
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
            if self.proc.returncode != 0:
                status, error = "failed", f"remote-control exited with status {self.proc.returncode}"
        except asyncio.CancelledError:
            status, error = "cancelled", self._stop_reason
        except Exception as e:
            status, error = "failed", f"{type(e).__name__}: {e}"
            self.log("error", traceback.format_exc())
        finally:
            await self._terminate()
            if master >= 0:
                asyncio.get_running_loop().remove_reader(master)
                os.close(master)
            self.store.finish_run(self.run_id, status, summary=self.link or "", error=error)

    async def _terminate(self) -> None:
        if not self.proc or self.proc.returncode is not None:
            return
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(asyncio.shield(self.proc.wait()), timeout=wait)
                return
            except asyncio.TimeoutError:
                continue


def _is_remote_control(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return False
    return "remote-control" in out


class WorkerManager:
    def __init__(self, store: Store, home: Path):
        self.store, self.home = store, home
        self.workers: dict[int, RemoteWorker] = {}

    def reap_orphans(self) -> None:
        """Stop workers left running by a previous bugsbot process (call before marking runs interrupted)."""
        for pid in self.store.orphaned_worker_pids():
            if _is_remote_control(pid):
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass

    def open(self, repo: str, branch: str, name: str) -> int:
        label = name.strip() or (repo.split("/")[-1] if repo else "scratch")
        run_id = self.store.start_worker(repo, f"worker: {label}")
        session_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"bugsbot-{run_id}-{label}")[:60]
        worker = RemoteWorker(self.store, self.home, run_id, repo, branch, session_name, name.strip())
        worker.task.add_done_callback(lambda _: self.workers.pop(run_id, None))
        self.workers[run_id] = worker
        return run_id

    def get(self, run_id: int) -> RemoteWorker | None:
        return self.workers.get(run_id)

    async def stop(self) -> None:
        for w in list(self.workers.values()):
            w.stop("bugsbot shut down")
        await asyncio.gather(*(w.task for w in self.workers.values()), return_exceptions=True)
