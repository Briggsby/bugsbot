import asyncio
import json
import os
import stat
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from bugsbot import remote
from bugsbot.agent import guardrails
from bugsbot.app import create_app
from bugsbot.store import Store

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import os, sys, time, json
assert sys.stdin.isatty() and sys.stdout.isatty(), "needs a terminal"
assert "ANTHROPIC_API_KEY" not in os.environ
json.dump(sys.argv[1:], open(os.environ["FAKE_ARGS"], "w"))
sys.stdout.write("\x1b[1mDo you trust the files in this folder?\x1b[0m\r\n> 1. Yes, proceed\r\n"); sys.stdout.flush()
sys.stdin.readline()
sys.stdout.write("Remote Control active: https://claude.ai/code/session_01FAKE\r\n"); sys.stdout.flush()
time.sleep(60)
"""


def test_guardrails_install(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"theirs": {"command": "x"}}}))
    subprocess.run(["git", "add", ".mcp.json"], cwd=cwd, check=True)

    for _ in range(2):  # idempotent
        guardrails.install(cwd, tmp_path / "home", 7, "o/r", "extra rule")
    settings = json.loads((cwd / ".claude/settings.local.json").read_text())
    hooks = settings["hooks"]["PreToolUse"]
    assert len(hooks) == 1 and "bugsbot.agent.hook" in hooks[0]["hooks"][0]["command"]
    assert settings["enabledMcpjsonServers"] == ["bugsbot"]
    servers = json.loads((cwd / ".mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"theirs", "bugsbot"} and "--run-id" in servers["bugsbot"]["args"]
    rules = (cwd / "CLAUDE.local.md").read_text()
    assert rules.count(guardrails.MARKER) == 1 and "NEVER merge" in rules and "extra rule" in rules
    # Nothing bugsbot wrote shows up for the agent to commit.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True).stdout
    assert status.strip() == "A  .mcp.json"  # staged before install; worktree edits hidden by skip-worktree


def test_guardrails_without_repo_has_no_tools(tmp_path):
    guardrails.install(tmp_path, tmp_path / "home", 1, None)
    assert not (tmp_path / ".mcp.json").exists()
    assert "enabledMcpjsonServers" not in json.loads((tmp_path / ".claude/settings.local.json").read_text())


def _hook(tmp_path, command):
    proc = subprocess.run([sys.executable, "-m", "bugsbot.agent.hook", "--home", str(tmp_path), "--run-id", "3"],
                          input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
                          capture_output=True, text=True, check=True)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def test_command_hook(tmp_path):
    out = _hook(tmp_path, "gh pr merge 1")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _hook(tmp_path, "ls -la") == {}
    assert Store(tmp_path).events(3)[0]["type"] == "blocked"


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
        manager = remote.WorkerManager(store, tmp_path / "home")
        run_id = manager.open("", "", "my task")
        worker = manager.get(run_id)
        for _ in range(100):
            if worker.link:
                break
            await asyncio.sleep(0.1)
        assert worker.link == "https://claude.ai/code/session_01FAKE"
        assert worker.name == f"bugsbot-{run_id}-my-task"
        assert store.run(run_id)["worker_pid"] == worker.proc.pid
        worker.stop()
        await asyncio.wait_for(asyncio.gather(worker.task, return_exceptions=True), timeout=10)
        assert worker.proc.returncode is not None and run_id not in manager.workers
        return store, run_id

    store, run_id = asyncio.run(scenario())
    run = store.run(run_id)
    assert (run["status"], run["kind"], run["summary"]) == ("cancelled", "worker", "https://claude.ai/code/session_01FAKE")
    args = json.load(open(tmp_path / "args.json"))
    assert args[:3] == ["remote-control", "--spawn", "session"] and "bypassPermissions" in args
    events = [e["type"] + ":" + e["text"] for e in store.events(run_id)]
    assert any("Accepted the folder trust prompt" in e for e in events)
    assert any(e.startswith("terminal:Do you trust the files") for e in events)  # ANSI stripped
    assert (tmp_path / "home/workers" / f"worker-{run_id}" / "CLAUDE.local.md").exists()


def test_worker_api_requires_tracked_repo(tmp_path):
    app = create_app(tmp_path)
    c = TestClient(app)
    c.get(f"/auth/link?t={app.state.auth.mint_login_link()}")
    r = c.post("/api/workers", json={"repo": "someone/else"}, headers={"X-Bugsbot": "1"})
    assert r.status_code == 400
    assert c.post("/api/workers/999/stop", headers={"X-Bugsbot": "1"}).status_code == 404
    assert c.post("/api/chats", json={}, headers={"X-Bugsbot": "1"}).status_code == 404
