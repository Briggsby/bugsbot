import asyncio
import json
import os
import stat
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from bugsbot.agent.toolspec import ToolSpec
from bugsbot.app import create_app
from bugsbot.providers.base import AgentJob, WorkerRequest
from bugsbot.providers.cursor import agent, cloud, credentials, guardrails
from bugsbot.providers.cursor.api import CursorAPI
from bugsbot.store import Store
from bugsbot.workers import WorkerManager

FAKE_CURSOR = r"""#!/usr/bin/env python3
import json, os, sys
json.dump({"argv": sys.argv[1:], "key": os.environ.get("CURSOR_API_KEY"), "gh": os.environ.get("GH_TOKEN"),
           "cwd": os.getcwd()}, open(os.environ["FAKE_ARGS"], "w"))
if os.environ.get("FAKE_MODE") == "fail":
    print("Error: not authenticated", file=sys.stderr); sys.exit(1)
def emit(e): print(json.dumps({**e, "session_id": "s"}), flush=True)
emit({"type": "system", "subtype": "init", "model": "m"})
emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Looking."}]}})
emit({"type": "tool_call", "subtype": "started", "call_id": "1", "tool_call": {"shellToolCall": {"args": {"command": "ls"}}}})
emit({"type": "tool_call", "subtype": "completed", "call_id": "1",
      "tool_call": {"shellToolCall": {"args": {"command": "ls"}, "result": {"success": {"stdout": "a"}}}}})
print("not json", flush=True)
emit({"type": "result", "subtype": "success", "duration_ms": 2000, "is_error": False, "result": "All done."})
"""


def _install_fake_cursor(tmp_path, monkeypatch, mode=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "cursor-agent"
    fake.write_text(FAKE_CURSOR)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("FAKE_MODE", mode)


def _job(tmp_path, prompt="Fix the bug"):
    home, cwd = tmp_path / "home", tmp_path / "repo"
    cwd.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    store = Store(home)
    store.update_settings({"cursor_api_key": "key_123", "cursor_model": "gpt-9"})
    spec = ToolSpec(kind="issue", repo="o/r", cwd=str(cwd), base_branch="main", protected=["main"], number=7)
    return AgentJob(store=store, home=home, run_id=1, cwd=cwd, prompt=prompt, rules="RULES", tools=[],
                    tool_spec=spec, log=lambda type_, text: store.add_event(1, type_, text))


def test_guardrails_install(tmp_path):
    cwd = tmp_path / "repo"
    (cwd / ".cursor").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
    (cwd / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"theirs": {"command": "x"}}}))
    for run_id in (1, 2):  # the checkout is reused by the next run
        guardrails.install(cwd, tmp_path / "home", run_id, tmp_path / f"spec{run_id}.json")
    servers = json.loads((cwd / ".cursor/mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"theirs", "bugsbot"} and servers["bugsbot"]["args"][-1] == str(tmp_path / "spec2.json")
    hooks = json.loads((cwd / ".cursor/hooks.json").read_text())
    shell = hooks["hooks"]["beforeShellExecution"]
    assert hooks["version"] == 1 and len(shell) == 1
    assert "--run-id 2" in shell[0]["command"] and "--format cursor" in shell[0]["command"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True).stdout
    assert status == ""  # hidden from the agent's commits


def test_cli_run(tmp_path, monkeypatch):
    _install_fake_cursor(tmp_path, monkeypatch)
    job = _job(tmp_path)
    out = asyncio.run(agent.run_agent(job))
    assert out.ok and out.result == "All done." and out.cost_usd is None
    seen = json.load(open(tmp_path / "args.json"))
    argv = seen["argv"]
    assert argv[:3] == ["-p", "--output-format", "stream-json"]
    assert {"--force", "--trust", "--approve-mcps"} <= set(argv)
    assert argv[argv.index("--workspace") + 1] == str(job.cwd) and argv[argv.index("--model") + 1] == "gpt-9"
    assert argv[-1].startswith("RULES") and argv[-1].endswith("Fix the bug")
    assert seen["key"] == "key_123" and seen["gh"] == ""  # Cursor key in, GitHub credentials out
    events = [(e["type"], e["text"]) for e in job.store.events(1)]
    assert ("assistant", "Looking.") in events and ("tool_use", 'shell {"command": "ls"}') in events
    assert ("stdout", "not json") in events and any(t == "tool_result" for t, _ in events)
    assert (job.home / "runs/1/tools.json").exists() and (job.cwd / ".cursor/hooks.json").exists()


def test_cli_run_failure_and_long_prompt(tmp_path, monkeypatch):
    _install_fake_cursor(tmp_path, monkeypatch, mode="fail")
    job = _job(tmp_path, prompt="x" * (agent.MAX_PROMPT_ARG + 1))
    out = asyncio.run(agent.run_agent(job))
    assert not out.ok and "not authenticated" in out.error
    argv = json.load(open(tmp_path / "args.json"))["argv"]
    assert "runs/1/prompt.md" in argv[-1] and (job.home / "runs/1/prompt.md").read_text().endswith("x")


def test_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "find_cli", lambda: None)
    out = asyncio.run(agent.run_agent(_job(tmp_path)))
    assert not out.ok and "isn't installed" in out.error


# -- Cloud Agents -------------------------------------------------------------------

class FakeCursorCloud:
    """An in-memory Cloud Agents API, served through httpx.MockTransport."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.agent = {"id": "bc-1", "status": "ACTIVE", "url": "https://cursor.com/agents?id=bc-1",
                      "latestRunId": "run-1"}
        self.run = {"id": "run-1", "status": "RUNNING"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        if request.headers.get("authorization") != httpx.BasicAuth("key_123", "")._auth_header:
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path
        if request.method == "POST" and path == "/v1/agents":
            return httpx.Response(200, json={"agent": self.agent, "run": self.run})
        if path == "/v1/agents":
            return httpx.Response(200, json={"items": []})
        if path == "/v1/agents/bc-1":
            return httpx.Response(200, json=self.agent)
        if path == "/v1/agents/bc-1/runs/run-1":
            return httpx.Response(200, json=self.run)
        if path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "run-1"})
        if path.endswith("/archive"):
            self.agent["status"] = "ARCHIVED"
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def api(self, key):
        return CursorAPI(key, transport=httpx.MockTransport(self.handler))


class FakeGH:
    created: list = []

    def __init__(self, token):
        self.token = token

    async def repo(self, name):
        return {"default_branch": "main"}

    async def branch_exists(self, repo, branch):
        return branch == "existing"

    async def create_branch(self, repo, branch, from_branch):
        FakeGH.created.append((branch, from_branch))

    async def close(self):
        pass


@pytest.fixture
def cursor_cloud(monkeypatch):
    fake = FakeCursorCloud()
    monkeypatch.setattr(cloud, "CursorAPI", fake.api)
    monkeypatch.setattr(cloud, "GitHub", FakeGH)
    monkeypatch.setattr(cloud, "resolve_token", lambda t: "tok")
    monkeypatch.setattr(cloud, "POLL_SECONDS", 0.05)
    FakeGH.created = []
    return fake


async def _until(cond, timeout=5):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def _store(tmp_path):
    store = Store(tmp_path)
    store.update_settings({"cursor_api_key": "key_123", "cursor_model": "composer-9"})
    return store


@pytest.mark.parametrize("branch,starting_ref,on_branch,created", [
    ("", "main", False, []),
    ("existing", "existing", True, []),
    ("new-topic", "new-topic", True, [("new-topic", "main")]),
])
def test_cloud_worker_launch_and_stop(tmp_path, cursor_cloud, branch, starting_ref, on_branch, created):
    async def scenario():
        store = _store(tmp_path)
        manager = WorkerManager(store, tmp_path)
        run_id = manager.open("cursor", "o/r", branch, "flaky tests", "Fix the flaky tests")
        worker = manager.get(run_id)
        await _until(lambda: worker.link)
        worker.stop()
        await asyncio.gather(worker.task, return_exceptions=True)
        return store, run_id

    store, run_id = asyncio.run(scenario())
    method, path, body = cursor_cloud.calls[0]
    assert (method, path) == ("POST", "/v1/agents")
    assert body["repos"] == [{"url": "https://github.com/o/r", "startingRef": starting_ref}]
    assert body["workOnCurrentBranch"] is on_branch and body["autoCreatePR"] is False
    assert body["model"] == {"id": "composer-9"} and body["name"] == "flaky tests"
    assert body["prompt"]["text"].startswith("Fix the flaky tests") and "Never merge" in body["prompt"]["text"]
    assert FakeGH.created == created
    run = store.run(run_id)
    assert (run["status"], run["provider"], run["external_id"]) == ("cancelled", "cursor", "bc-1")
    assert run["summary"] == "https://cursor.com/agents?id=bc-1"
    assert ("POST", "/v1/agents/bc-1/runs/run-1/cancel", {}) in cursor_cloud.calls
    assert ("POST", "/v1/agents/bc-1/archive", {}) in cursor_cloud.calls


def test_cloud_worker_refuses_protected_branch(tmp_path, cursor_cloud):
    async def scenario():
        store = _store(tmp_path)
        run_id = store.start_worker("o/r", "worker: x", "cursor")
        w = cloud.CloudWorker(store, tmp_path, run_id, WorkerRequest("o/r", "main", "", "s", "task"))
        await asyncio.gather(w.task, return_exceptions=True)
        return store.run(run_id)
    run = asyncio.run(scenario())
    assert run["status"] == "failed" and "protected" in run["error"] and not cursor_cloud.calls


def test_cloud_worker_survives_restart_and_ends_when_archived(tmp_path, cursor_cloud):
    async def first_process():
        store = _store(tmp_path)
        manager = WorkerManager(store, tmp_path)
        run_id = manager.open("cursor", "o/r", "", "", "Do it")
        await _until(lambda: manager.get(run_id).link)
        await manager.stop()  # bugsbot shuts down: the cloud agent keeps going
        return run_id

    run_id = asyncio.run(first_process())
    store = Store(tmp_path)
    assert store.run(run_id)["status"] == "running"
    assert not any(path.endswith("/archive") for _, path, _ in cursor_cloud.calls)
    store.mark_interrupted_runs()  # what the harness does at start-up
    assert store.run(run_id)["status"] == "running"

    async def second_process():
        manager = WorkerManager(store, tmp_path)
        manager.recover()
        worker = manager.get(run_id)
        assert worker and worker.link == "https://cursor.com/agents?id=bc-1"
        cursor_cloud.run["status"] = "FINISHED"
        cursor_cloud.run["git"] = {"branches": [{"repoUrl": "github.com/o/r", "branch": "cursor/x",
                                                 "prUrl": "https://github.com/o/r/pull/9"}]}
        await _until(lambda: any("pull/9" in e["text"] for e in store.events(run_id)))
        cursor_cloud.agent["status"] = "ARCHIVED"  # the maintainer archives it in Cursor
        await asyncio.wait_for(worker.task, timeout=5)

    asyncio.run(second_process())
    run = store.run(run_id)
    assert run["status"] == "succeeded" and run["ended_at"]
    assert any("archived in Cursor" in e["text"] for e in store.events(run_id))


def test_check_api_key(cursor_cloud):
    assert asyncio.run(credentials.check_api_key("key_123", cursor_cloud.api("key_123"))) is None
    assert "rejected" in asyncio.run(credentials.check_api_key("bad", cursor_cloud.api("bad")))


def test_worker_api_validates_cursor_requests(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    Store(tmp_path).add_repo("o/r")
    c = TestClient(app)
    c.get(f"/auth/link?t={app.state.auth.mint_login_link()}")
    h = {"X-Bugsbot": "1"}
    post = lambda body: c.post("/api/workers", json={"provider": "cursor", **body}, headers=h)  # noqa: E731
    assert "needs a repository" in post({"task": "x"}).json()["detail"]
    assert "first message" in post({"repo": "o/r"}).json()["detail"]
    assert "no Cursor API key" in post({"repo": "o/r", "task": "x"}).json()["detail"]
    assert c.post("/api/workers", json={"provider": "nope"}, headers=h).status_code == 400
    assert c.get("/api/workers/account?provider=cursor").json()["ok"] is False
    state = c.get("/api/state").json()
    assert {p["name"] for p in state["providers"]} == {"claude", "cursor"}
    assert c.put("/api/settings", json={"agent_provider": "nope"}, headers=h).status_code == 400
    assert c.put("/api/settings", json={"agent_provider": "cursor"}, headers=h).json()["agent_provider"] == "cursor"
