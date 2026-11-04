import asyncio
from types import SimpleNamespace

from claude_agent_sdk import ResultMessage

from bugsbot.agent.prompts import issue_prompt, pr_prompt
from bugsbot.agent.run import AgentRun, RunContext
from bugsbot.agent.tools import ToolKit
from bugsbot.github import is_bugsbot, replied_keys
from bugsbot.store import Store
from bugsbot.work import PendingComment, Work
from bugsbot.workspace import GitError


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.open_pr = None

    async def create_comment(self, repo, number, body):
        self.calls.append(("comment", number, body))
        return {"html_url": f"https://gh/c/{len(self.calls)}"}

    async def reply_review_comment(self, repo, pr, comment_id, body):
        self.calls.append(("thread_reply", comment_id, body))
        return {"html_url": "https://gh/r"}

    async def find_open_pr(self, repo, branch):
        return self.open_pr

    async def create_pr(self, repo, head, base, title, body, draft):
        self.calls.append(("create_pr", head, base, body))
        return {"number": 42, "title": title, "html_url": "https://gh/pull/42"}

    async def add_labels(self, repo, number, labels):
        self.calls.append(("labels", number, labels))


class FakeWorkspace:
    def __init__(self, branch="bugsbot/issue-7"):
        self.branch = branch

    async def push(self, protected):
        if self.branch in protected:
            raise GitError(f"Refusing to push to protected branch '{self.branch}'")
        return f"Pushed {self.branch}"

    async def current_branch(self):
        return self.branch


def kit(gh, ws=None):
    return ToolKit(gh=gh, repo="o/r", signature="SIG", workspace=ws or FakeWorkspace(),
                   protected={"main"}, base_branch="main", log=lambda *_: None)


def call(t, **args):
    return asyncio.run(t.handler(args))


def pending(key, kind, target):
    return PendingComment(key=key, kind=kind, author="bob", body="please fix\nmore", url="https://u",
                          created_at="t", reply_target=target)


def test_reply_routes_threads_and_quotes_others():
    gh, replied = FakeGitHub(), set()
    t = kit(gh).reply_to_comment(5, [pending("thread:11", "thread", 10), pending("issue:3", "issue", 3)], replied)
    call(t, comment_key="thread:11", body="agreed")
    call(t, comment_key="issue:3", body="done")
    (k1, target, body1), (k2, number, body2) = gh.calls
    assert (k1, target) == ("thread_reply", 10)  # reply goes to the thread root
    assert (k2, number) == ("comment", 5) and body2.startswith("> @bob: please fix")
    assert all(is_bugsbot(b) and "SIG" in b for b in (body1, body2))
    assert replied_keys(body1) == {"thread:11"} and replied == {"thread:11", "issue:3"}
    assert call(t, comment_key="issue:999", body="x")["is_error"]


def test_open_pr_fixed_labels_and_resolves():
    gh, seen = FakeGitHub(), []
    t = kit(gh).open_pull_request(labels=["ai"], resolves=7, on_opened=lambda pr, new, labels: seen.append((new, labels)))
    assert "labels" not in t.input_schema["properties"]
    call(t, title="Fix", body="Body")
    _, head, base, body = gh.calls[0]
    assert (head, base) == ("bugsbot/issue-7", "main") and "Resolves #7" in body and "SIG" in body
    assert gh.calls[1] == ("labels", 42, ["ai"]) and seen == [(True, ["ai"])]


def test_open_pr_existing_and_agent_labels():
    gh = FakeGitHub()
    gh.open_pr = {"number": 9, "title": "t", "html_url": "u"}
    t = kit(gh).open_pull_request(labels=None)
    out = call(t, title="x", body="y", labels=["bug"])
    assert "Updated existing PR #9" in out["content"][0]["text"]
    assert gh.calls == [("labels", 9, ["bug"])]


def test_push_to_protected_branch_is_an_error():
    out = call(kit(FakeGitHub(), FakeWorkspace("main")).push_branch())
    assert out["is_error"] and "protected" in out["content"][0]["text"]


def test_comment_with_and_without_fixed_number():
    gh = FakeGitHub()
    assert "number" not in kit(gh).comment(5).input_schema["properties"]
    call(kit(gh).comment(), number=8, body="hi")
    assert gh.calls[0][:2] == ("comment", 8)


def test_prompts_include_pending_keys():
    p = pending("thread:11", "thread", 10)
    issue = {"number": 7, "title": "Bug", "html_url": "u", "user": {"login": "a"}, "body": "broken"}
    text = issue_prompt("o/r", issue, [], [p], "bugsbot/issue-7", "main", [42])
    assert "[thread:11]" in text and "#42" in text and "reply_to_comment" in text
    pr = {"number": 3, "title": "T", "url": "u", "baseRefName": "main", "headRefName": "f",
          "isCrossRepository": True, "body": ""}
    text = pr_prompt("o/r", pr, [p])
    assert "[thread:11]" in text and "from a fork" in text


def _run(tmp_path, kind, pending_keys, replied):
    store = Store(tmp_path)
    item = store.upsert_item("o/r", 3, kind, "t", "u", [], "")
    work = Work("x", "k", [pending(k, "issue", 1) for k in pending_keys])
    run = AgentRun(RunContext(store, None, tmp_path, 1, item, work, []))
    run.replied = set(replied)
    return run


def _result(is_error=False):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=4, session_id="s", total_cost_usd=0.5, result="done")


def test_outcome_requires_every_pr_comment_answered(tmp_path):
    out = _run(tmp_path, "pr", ["issue:1", "issue:2"], ["issue:1"])._outcome(_result())
    assert out.status == "failed" and "issue:2" in out.summary
    out = _run(tmp_path, "pr", ["issue:1"], ["issue:1"])._outcome(_result())
    assert (out.status, out.num_turns, out.cost_usd) == ("succeeded", 4, 0.5)
    assert _run(tmp_path, "issue", [], [])._outcome(None).status == "failed"
    assert _run(tmp_path, "issue", [], [])._outcome(_result(is_error=True)).status == "failed"


def test_outcome_explains_login_failures(tmp_path):
    res = _result(is_error=True)
    res.result = "Invalid API key · Please run /login"
    out = _run(tmp_path, "issue", [], [])._outcome(res)
    assert out.status == "failed" and out.summary.startswith("Claude Code had no working credentials")
