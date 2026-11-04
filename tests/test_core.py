import asyncio

import pytest
from fastapi.testclient import TestClient

from bugsbot.app import create_app
from bugsbot.auth import LINK_USER, Auth
from bugsbot.github import MARKER, is_bugsbot, replied_keys, sign
from bugsbot.store import DEFAULT_SIGNATURE, Store
from bugsbot.authors import association_policy
from bugsbot.providers.base import AgentJob
from bugsbot.providers.claude.agent import ClaudeAgent
from bugsbot.work import issue_work, pr_work

TRUSTED, ANYONE = association_policy(True), association_policy(False)


# -- signing ---------------------------------------------------------------------

def test_sign_appends_signature_and_markers():
    body = sign("Hello", "— my bot", reply_to=["thread:5", "issue:7"])
    assert body.startswith("Hello\n\n---\n— my bot\n")
    assert is_bugsbot(body)
    assert replied_keys(body) == {"thread:5", "issue:7"}


def test_sign_never_drops_signature():
    assert DEFAULT_SIGNATURE in sign("x", "   ")


# -- work detection ----------------------------------------------------------------

def _rest_comment(id_, body, login="alice", assoc="OWNER", typ="User"):
    return {"id": id_, "body": body, "user": {"login": login, "type": typ}, "author_association": assoc,
            "html_url": f"https://x/{id_}", "created_at": f"2026-01-01T00:00:{id_:02d}Z"}


def test_issue_initial_then_followups():
    assert issue_work([], TRUSTED).trigger_key == "initial"
    bot = _rest_comment(1, f"on it {MARKER}")
    assert issue_work([bot], TRUSTED) is None
    w = issue_work([bot, _rest_comment(2, "any update?"), _rest_comment(3, "spam", assoc="NONE"),
                    _rest_comment(4, "ci", login="ci[bot]", typ="Bot")], TRUSTED)
    assert [p.key for p in w.pending] == ["issue:2"]
    assert len(issue_work([bot, _rest_comment(3, "hi", assoc="NONE")], ANYONE).pending) == 1


def _node(id_, body, login="bob", assoc="MEMBER", reply_to=None):
    return {"databaseId": id_, "body": body, "createdAt": f"2026-01-01T00:00:{id_:02d}Z", "url": f"u{id_}",
            "authorAssociation": assoc, "author": {"login": login, "__typename": "User"}, "diffHunk": "@@"}


def _pr(comments=(), reviews=(), threads=()):
    return {"comments": {"nodes": list(comments)}, "reviews": {"nodes": list(reviews)},
            "reviewThreads": {"nodes": list(threads)}}


def _thread(*nodes, resolved=False):
    return {"isResolved": resolved, "isOutdated": False, "path": "a.py", "line": 3,
            "comments": {"nodes": list(nodes)}}


def test_pr_work_finds_unanswered_comments():
    pr = _pr(
        comments=[_node(1, "please rename"), _node(2, sign("done", "", reply_to=["issue:9"]))],
        reviews=[{**_node(3, "looks off"), "state": "CHANGES_REQUESTED"}, {**_node(4, ""), "state": "COMMENTED"}],
        threads=[
            _thread(_node(10, "nit: typo"), _node(11, sign("fixed", ""))),       # answered
            _thread(_node(20, "why?"), _node(21, sign("because", "")), _node(22, "not convinced")),
            _thread(_node(30, "resolved one"), resolved=True),
        ],
    )
    w = pr_work(pr, TRUSTED)
    keys = {p.key: p for p in w.pending}
    assert set(keys) == {"issue:1", "review:3", "thread:22"}
    assert keys["thread:22"].reply_target == 20  # replies go to the thread root


def test_pr_work_respects_reply_markers():
    pr = _pr(comments=[_node(1, "please rename"), _node(2, sign("ok", "", reply_to=["issue:1"]))])
    assert pr_work(pr, TRUSTED) is None


# -- guard -------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,blocked", [
    ("gh pr merge 3 --squash", True),
    ("gh api -X PUT repos/o/r/pulls/3/merge", True),
    ("curl -X PUT https://api.github.com/repos/o/r/pulls/3/merge", True),
    ("git push origin main", True),
    ("cd x && git push", True),
    ("gh pr create --title t", True),
    ("git commit -m 'merge sort'", False),
    ("pytest -q", False),
    ("git log --merges", False),
])
def test_guard(cmd, blocked):
    events = []
    job = AgentJob(store=None, home=None, run_id=1, cwd=None, prompt="", rules="", tools=[], tool_spec=None,
                   log=lambda t, x: events.append(t))
    out = asyncio.run(ClaudeAgent(job).guard({"tool_input": {"command": cmd}}, None, None))
    assert (out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny") is blocked
    assert (events == ["blocked"]) is blocked


# -- auth ----------------------------------------------------------------------------

def link_client(app):
    c = TestClient(app)
    assert c.get(f"/auth/link?t={app.state.auth.mint_login_link()}").status_code == 200
    return c


def test_sessions(tmp_path):
    auth = Auth(Store(tmp_path))
    cookie = auth.new_session("gh:alice")
    assert auth.session_user(cookie) == "gh:alice"
    assert not auth.valid_session(cookie + "x") and not auth.valid_session(cookie.replace("alice", "mallory"))
    assert not auth.valid_session(auth.new_session("gh:alice", ttl=-1))
    auth.sign_out_everyone()
    assert not auth.valid_session(cookie)


def test_api_requires_auth(tmp_path, monkeypatch):
    async def key_ok(key, model):
        return None
    monkeypatch.setattr("bugsbot.providers.claude.credentials.check_api_key", key_ok)
    app = create_app(tmp_path)
    anon = TestClient(app)
    assert anon.get("/api/state").status_code == 401
    assert anon.get("/", follow_redirects=False).status_code == 303
    assert anon.get("/login").status_code == 200
    assert anon.post("/api/login", json={"password": "x"}, headers={"X-Bugsbot": "1"}).status_code == 401

    c = link_client(app)
    h = {"X-Bugsbot": "1"}
    assert c.post("/api/labels", json={"name": "ai"}).status_code == 403  # CSRF header missing
    state = c.get("/api/state").json()
    assert state["viewer"] == LINK_USER
    assert "session_secret" not in state["settings"] and "api_token_hash" not in state["settings"]
    assert c.put("/api/settings", json={"session_secret": "x"}, headers=h).status_code == 400
    c.put("/api/settings", json={"anthropic_api_key": "  sk-ant-test\n", "poll_interval_seconds": 30}, headers=h)
    s = c.get("/api/state").json()["settings"]
    assert s["has_anthropic_api_key"] and "anthropic_api_key" not in s and s["poll_interval_seconds"] == 30
    assert c.post("/api/labels", json={"name": "ai"}, headers=h).json() == ["ai"]

    token = c.post("/api/auth/token", headers=h).json()["token"]
    assert anon.get("/api/state", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert anon.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_login_link_single_use_and_shared_via_db(tmp_path):
    # Links minted by a separate process (`bugsbot login-link`) work on the running server.
    t = Auth(Store(tmp_path)).mint_login_link()
    app = create_app(tmp_path)
    c = TestClient(app)
    r = c.get(f"/auth/link?t={t}", follow_redirects=False)
    assert r.status_code == 200 and 'location.replace("/")' in r.text
    assert c.get("/api/state").status_code == 200
    assert TestClient(app).get(f"/auth/link?t={t}", follow_redirects=False).headers["location"] == "/login"
    assert TestClient(app).get("/auth/link?t=", follow_redirects=False).headers["location"] == "/login"


def test_legacy_settings_ignored(tmp_path):
    store = Store(tmp_path)
    store._x("INSERT INTO settings VALUES ('admin_password_hash', '\"scrypt$x$y\"')")
    assert "admin_password_hash" not in store.settings()


def test_bad_api_key_is_rejected_on_save(tmp_path, monkeypatch):
    seen = []
    async def key_bad(key, model):
        seen.append((key, model))
        return "Anthropic rejected this API key"
    monkeypatch.setattr("bugsbot.providers.claude.credentials.check_api_key", key_bad)
    app = create_app(tmp_path)
    c = link_client(app)
    r = c.put("/api/settings", json={"anthropic_api_key": " sk-ant-bad "}, headers={"X-Bugsbot": "1"})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    assert seen == [("sk-ant-bad", "claude-opus-5-5")]  # whitespace stripped before checking
    assert not c.get("/api/state").json()["settings"]["has_anthropic_api_key"]


def test_check_api_key_format():
    import asyncio
    from bugsbot.providers.claude.credentials import check_api_key, needs_login
    assert "sk-ant-" in asyncio.run(check_api_key("abc123", "claude-opus-5-5"))
    assert needs_login("Invalid API key · Please run /login")
    assert needs_login("Not logged in · Please run /login") and not needs_login("all good")
    assert needs_login("Error: You must be logged in to use Remote Control")


def test_credentials_check_and_save(tmp_path, monkeypatch):
    async def key_check(key, model):
        return None if key == "sk-ant-good" else "Anthropic rejected this API key"

    class FakeGH:
        def __init__(self, token):
            self.token = token
        async def viewer(self):
            if self.token != "ghp_good":
                from bugsbot.github import GitHubError
                raise GitHubError("401 Bad credentials")
            return {"login": "octocat"}
        async def close(self):
            pass

    monkeypatch.setattr("bugsbot.providers.claude.credentials.check_api_key", key_check)
    monkeypatch.setattr("bugsbot.app.GitHub", FakeGH)
    monkeypatch.setattr("bugsbot.app.resolve_token", lambda configured: configured or "")
    app = create_app(tmp_path)
    c = link_client(app)
    h = {"X-Bugsbot": "1"}

    status = c.get("/api/credentials").json()
    assert status["anthropic_api_key"]["ok"] is None and status["github_token"]["ok"] is False

    r = c.post("/api/credentials", json={"anthropic_api_key": " sk-ant-good ", "github_token": "ghp_bad"}, headers=h).json()
    assert r["anthropic_api_key"] == {"ok": True, "detail": "works with claude-opus-5-5", "saved": True}
    assert r["github_token"]["ok"] is False and r["github_token"]["saved"] is False
    assert r["github_token"]["detail"].startswith("not saved:")
    s = c.get("/api/state").json()["settings"]
    assert s["has_anthropic_api_key"] and not s["has_github_token"]

    r = c.post("/api/credentials", json={"github_token": "ghp_good"}, headers=h).json()
    assert r["github_token"]["ok"] and "@octocat" in r["github_token"]["detail"]
    assert r["anthropic_api_key"]["ok"] is True and "saved" not in r["anthropic_api_key"]  # re-checked, untouched
    assert c.get("/api/credentials").json()["github_token"]["ok"] is True
