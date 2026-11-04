import asyncio
import json
import os
import stat
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from bugsbot.app import create_app
from bugsbot.providers.claude import guardrails
from bugsbot.providers.claude import remote_control as remote
from bugsbot.store import Store
from bugsbot.workers import WorkerManager

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import os, sys, time, json
if sys.argv[1:3] == ["auth", "status"]:
    assert "ANTHROPIC_API_KEY" not in os.environ
    print(os.environ.get("FAKE_AUTH", "{}")); sys.exit(0)
assert sys.stdin.isatty() and sys.stdout.isatty(), "needs a terminal"
assert "ANTHROPIC_API_KEY" not in os.environ
json.dump(sys.argv[1:], open(os.environ["FAKE_ARGS"], "w"))
if os.environ.get("FAKE_MODE") == "untrusted":
    sys.stdout.write("\x1b[1mDo you \x1b[0mtrust\x1b[1m the files in this \x1b[0m"); sys.stdout.flush()
    time.sleep(0.2)
    sys.stdout.write("folder?\r\n> 1. Yes, proceed\r\n"); sys.stdout.flush()  # split across reads
    sys.stdin.readline()
    time.sleep(60)
mode = os.environ.get("FAKE_MODE", "")
if mode.startswith("enable-"):
    import tty
    tty.setraw(0)  # like Claude Code: single keypresses, no Enter needed
    if mode == "enable-menu":
        sys.stdout.write("\x1b[1mEnable Remote Control?\x1b[0m\r\n"); sys.stdout.flush()
        time.sleep(0.2)  # options drawn separately
        sys.stdout.write("\x1b[36m\u276f 1. Yes, enable\x1b[0m\r\n  2. No\r\n"); sys.stdout.flush()
        expected = "1"
    else:
        sys.stdout.write("Enable Remote Control for this account? (y/n) "); sys.stdout.flush()
        expected = "y"
    got = sys.stdin.read(1)
    open(os.environ["FAKE_ARGS"] + ".key", "w").write(got)
    if got != expected:
        sys.exit(3)
sys.stdout.write("\x1b[1mRemote Control\x1b[0m starting\r\n"); sys.stdout.flush()
if os.environ.get("FAKE_MODE") == "never-connect":
    time.sleep(60)
sys.stdout.write("Remote Control active: https://claude.ai/code/session_01FAKE\r\n"); sys.stdout.flush()
time.sleep(0.5)
sys.stdout.write("user: PRIVATE CHAT MESSAGE\r\n"); sys.stdout.flush()
time.sleep(60)
"""


def test_guardrails_install(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"theirs": {"command": "x"}}}))
    subprocess.run(["git", "add", ".mcp.json"], cwd=cwd, check=True)

    for _ in range(2):  # idempotent
        guardrails.install(cwd, tmp_path / "home", 7, tmp_path / "spec.json", "extra rule")
    settings = json.loads((cwd / ".claude/settings.local.json").read_text())
    hooks = settings["hooks"]["PreToolUse"]
    assert len(hooks) == 1 and "bugsbot.agent.hook" in hooks[0]["hooks"][0]["command"]
    assert settings["enabledMcpjsonServers"] == ["bugsbot"]
    servers = json.loads((cwd / ".mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"theirs", "bugsbot"} and "--run-id" in servers["bugsbot"]["args"]
    assert servers["bugsbot"]["args"][-2:] == ["--spec", str(tmp_path / "spec.json")]
    rules = (cwd / "CLAUDE.local.md").read_text()
    assert rules.count(guardrails.MARKER) == 1 and "NEVER merge" in rules and "extra rule" in rules
    # Nothing bugsbot wrote shows up for the agent to commit.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True).stdout
    assert status.strip() == "A  .mcp.json"  # staged before install; worktree edits hidden by skip-worktree


def test_guardrails_without_repo_has_no_tools(tmp_path):
    guardrails.install(tmp_path, tmp_path / "home", 1, None)
    assert not (tmp_path / ".mcp.json").exists()
    assert "enabledMcpjsonServers" not in json.loads((tmp_path / ".claude/settings.local.json").read_text())


def _hook(tmp_path, command, fmt="claude", flag="--provider"):
    payload = {"command": command, "cwd": "/w"} if fmt == "cursor" else {"tool_name": "Bash",
                                                                         "tool_input": {"command": command}}
    proc = subprocess.run([sys.executable, "-m", "bugsbot.agent.hook", "--home", str(tmp_path), "--run-id", "3",
                           flag, fmt], input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def test_command_hook(tmp_path):
    out = _hook(tmp_path, "gh pr merge 1")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _hook(tmp_path, "ls -la") == {}
    assert Store(tmp_path).events(3)[0]["type"] == "blocked"


def test_command_hook_cursor_format(tmp_path):
    out = _hook(tmp_path, "git push origin main", "cursor")
    assert out["permission"] == "deny" and "push_branch" in out["agent_message"]
    assert _hook(tmp_path, "ls -la", "cursor") == {"permission": "allow"}  # Cursor needs an explicit answer
    # Hooks installed before the flag was renamed still work.
    assert _hook(tmp_path, "gh pr merge 1", "cursor", flag="--format")["permission"] == "deny"


def test_hook_does_not_import_provider_sdks():
    code = ("import sys, bugsbot.agent.hook as h; h.load('claude'); h.load('cursor'); "
            "print([m for m in sys.modules if m.startswith(('claude_agent_sdk', 'httpx'))])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_find_session_link(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    now = time.time()
    (tmp_path / "sessions/1.json").write_text(json.dumps({"cwd": "/w", "startedAt": now * 1000}))
    assert remote.find_session_link(tmp_path / "w", now) is None
    (tmp_path / "sessions/2.json").write_text(json.dumps(
        {"cwd": str(tmp_path / "w"), "startedAt": now * 1000, "bridgeSessionId": "session_01X"}))
    assert remote.find_session_link(tmp_path / "w", now) == "https://claude.ai/code/session_01X"
    assert remote.find_session_link(tmp_path / "w", now + 60) is None  # stale record from an older run


def test_worker_lifecycle_with_fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))

    async def scenario():
        store = Store(tmp_path / "home")
        manager = WorkerManager(store, tmp_path / "home")
        run_id = manager.open("claude", "", "", "my task")
        worker = manager.get(run_id)
        for _ in range(100):
            if worker.link:
                break
            await asyncio.sleep(0.1)
        assert worker.link == "https://claude.ai/code/session_01FAKE"
        assert worker.name == f"bugsbot-{run_id}-my-task"
        assert store.run(run_id)["worker_pid"] == worker.proc.pid
        projects = json.loads((tmp_path / "claude-home/.claude.json").read_text())["projects"]
        assert projects[str(worker.cwd.resolve())]["hasTrustDialogAccepted"] is True
        await asyncio.sleep(1.5)  # let the post-connect output arrive
        worker.stop()
        await asyncio.wait_for(asyncio.gather(worker.task, return_exceptions=True), timeout=10)
        assert worker.proc.returncode is not None and run_id not in manager.workers
        return store, run_id

    store, run_id = asyncio.run(scenario())
    run = store.run(run_id)
    assert (run["status"], run["kind"], run["summary"]) == ("cancelled", "worker", "https://claude.ai/code/session_01FAKE")
    assert run["error"] == "stopped from bugsbot"
    args = json.load(open(tmp_path / "args.json"))
    assert args[:3] == ["remote-control", "--spawn", "session"] and "bypassPermissions" in args
    events = [e["type"] + ":" + e["text"] for e in store.events(run_id)]
    assert "terminal:Remote Control starting" in events  # ANSI stripped
    assert json.loads((tmp_path / "claude-home/.claude.json").read_text())["projects"] == {}  # trust removed
    assert (tmp_path / "home/workers/scratch" / f"worker-{run_id}" / "CLAUDE.local.md").exists()
    assert not any("PRIVATE CHAT MESSAGE" in e for e in events)  # nothing recorded once connected
    assert any("controlled from the Claude app" in e for e in events)


def test_worker_api_requires_tracked_repo(tmp_path):
    app = create_app(tmp_path)
    c = TestClient(app)
    c.get(f"/auth/link?t={app.state.auth.mint_login_link()}")
    r = c.post("/api/workers", json={"repo": "someone/else"}, headers={"X-Bugsbot": "1"})
    assert r.status_code == 400
    assert c.post("/api/workers/999/stop", headers={"X-Bugsbot": "1"}).status_code == 404
    assert c.post("/api/chats", json={}, headers={"X-Bugsbot": "1"}).status_code == 404


class _FakeRepoGH:
    def __init__(self, token):
        self.token = token
    async def repo(self, name):
        return {"default_branch": "trunk"}
    async def close(self):
        pass


def _bare_worker(tmp_path, repo="o/r", branch="", label=""):
    w = remote.RemoteWorker.__new__(remote.RemoteWorker)  # without starting it
    w.store, w.home, w.run_id = Store(tmp_path), tmp_path, 5
    w.repo, w.branch, w.name, w.label, w.default_branch = repo, branch, "bugsbot-5", label, ""
    return w


@pytest.mark.parametrize("branch,expected_checkout", [("feature/new-thing", "feature/new-thing"), ("", "trunk")])
def test_worker_checkout_branch(tmp_path, monkeypatch, branch, expected_checkout):
    prepared = []

    async def fake_prepare(self, branch, base, name, email, pr_number=None):
        prepared.append((branch, base))
    monkeypatch.setattr(remote, "GitHub", _FakeRepoGH)
    monkeypatch.setattr(remote, "resolve_token", lambda t: "tok")
    monkeypatch.setattr(remote.Workspace, "prepare", fake_prepare)
    w = _bare_worker(tmp_path, branch=branch, label="flaky tests")
    asyncio.run(w._prepare_cwd())
    assert prepared == [(expected_checkout, "trunk")]
    rules = w.branch_rules()
    if branch:
        assert f"`{branch}`" in rules and "already checked out" in rules
    else:
        assert "start on `trunk`" in rules and "git checkout -b" in rules and '"flaky tests"' in rules


def test_worker_refuses_protected_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "GitHub", _FakeRepoGH)
    monkeypatch.setattr(remote, "resolve_token", lambda t: "tok")
    with pytest.raises(ValueError, match="protected"):
        asyncio.run(_bare_worker(tmp_path, branch="trunk")._prepare_cwd())


def test_scratch_worker_has_no_branch_rules(tmp_path):
    assert _bare_worker(tmp_path, repo="").branch_rules() == ""


def test_worker_api_validates_branch(tmp_path):
    app = create_app(tmp_path)
    Store(tmp_path).add_repo("o/r")
    c = TestClient(app)
    c.get(f"/auth/link?t={app.state.auth.mint_login_link()}")
    h = {"X-Bugsbot": "1"}
    assert "valid git branch" in c.post("/api/workers", json={"repo": "o/r", "branch": "bad..name"}, headers=h).json()["detail"]
    assert "needs a repository" in c.post("/api/workers", json={"branch": "x"}, headers=h).json()["detail"]


def _install_fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.parametrize("status,ok,detail", [
    ({"loggedIn": True, "authMethod": "claude.ai", "email": "a@b.co", "orgName": "Acme", "subscriptionType": "max"},
     True, "ready"),
    ({"loggedIn": False}, False, "not logged in"),
    ({"loggedIn": True, "authMethod": "api_key"}, False, "needs a Claude account"),
])
def test_claude_account(tmp_path, monkeypatch, status, ok, detail):
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_AUTH", json.dumps(status))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    out = asyncio.run(remote.claude_account(Store(tmp_path / "home"), tmp_path / "home"))
    assert out["ok"] is ok and detail in out["detail"]
    if ok:
        assert (out["email"], out["orgName"], out["subscriptionType"]) == ("a@b.co", "Acme", "max")


def test_claude_account_without_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    out = asyncio.run(remote.claude_account(Store(tmp_path), tmp_path))
    assert out["ok"] is False and "isn't installed" in out["detail"]


def test_worker_start_refused_when_not_logged_in(tmp_path, monkeypatch):
    async def logged_out(store, home):
        return {"ok": False, "detail": "not logged in"}
    monkeypatch.setattr(remote, "claude_account", logged_out)
    app = create_app(tmp_path)
    c = TestClient(app)
    c.get(f"/auth/link?t={app.state.auth.mint_login_link()}")
    r = c.post("/api/workers", json={}, headers={"X-Bugsbot": "1"})
    assert r.status_code == 400 and "/login" in r.json()["detail"]


def test_worker_that_never_connects_times_out(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("FAKE_MODE", "never-connect")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setattr(remote, "STARTUP_TIMEOUT", 1)

    async def scenario():
        store = Store(tmp_path / "home")
        manager = WorkerManager(store, tmp_path / "home")
        run_id = manager.open("claude", "", "", "")
        worker = manager.get(run_id)
        await asyncio.wait_for(asyncio.gather(worker.task, return_exceptions=True), timeout=15)
        assert worker.proc.returncode is not None  # process was terminated
        return store.run(run_id)

    run = asyncio.run(scenario())
    assert run["status"] == "failed" and "didn't connect" in run["error"]


def test_untrusted_worker_fails_fast_without_answering(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("FAKE_MODE", "untrusted")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))

    async def scenario():
        store = Store(tmp_path / "home")
        manager = WorkerManager(store, tmp_path / "home")
        run_id = manager.open("claude", "", "", "")
        worker = manager.get(run_id)
        start = time.time()
        await asyncio.wait_for(asyncio.gather(worker.task, return_exceptions=True), timeout=15)
        assert time.time() - start < 10  # well before the start-up timeout
        return store, run_id

    store, run_id = asyncio.run(scenario())
    run = store.run(run_id)
    assert run["status"] == "failed" and "asked to trust" in run["error"]
    errors = [e["text"] for e in store.events(run_id) if e["type"] == "error"]
    assert any("Start the worker again" in e for e in errors)


@pytest.mark.parametrize("mode,key,how", [("enable-menu", "1", "chose option 1"), ("enable-yn", "y", "pressed y")])
def test_worker_answers_enable_remote_control(tmp_path, monkeypatch, mode, key, how):
    _install_fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))

    async def scenario():
        store = Store(tmp_path / "home")
        manager = WorkerManager(store, tmp_path / "home")
        run_id = manager.open("claude", "", "", "")
        worker = manager.get(run_id)
        for _ in range(100):
            if worker.link or worker.task.done():
                break
            await asyncio.sleep(0.1)
        assert worker.link, [e["text"] for e in store.events(run_id)]
        worker.stop()
        await asyncio.gather(worker.task, return_exceptions=True)
        return store, run_id

    store, run_id = asyncio.run(scenario())
    assert (tmp_path / "args.json.key").read_text() == key
    assert any(how in e["text"] for e in store.events(run_id))
    config = json.loads((tmp_path / "claude-home/.claude.json").read_text())
    assert config["remoteDialogSeen"] is True
