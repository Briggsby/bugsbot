import asyncio

import pytest
from fastapi.testclient import TestClient

from bugsbot.agent import make_guard
from bugsbot.app import create_app
from bugsbot.auth import Auth, hash_password, verify_password
from bugsbot.github import MARKER, is_bugsbot, replied_keys, sign
from bugsbot.store import DEFAULT_SIGNATURE, Store
from bugsbot.work import issue_work, pr_work


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
    assert issue_work([], True).trigger_key == "initial"
    bot = _rest_comment(1, f"on it {MARKER}")
    assert issue_work([bot], True) is None
    w = issue_work([bot, _rest_comment(2, "any update?"), _rest_comment(3, "spam", assoc="NONE"),
                    _rest_comment(4, "ci", login="ci[bot]", typ="Bot")], True)
    assert [p.key for p in w.pending] == ["issue:2"]
    assert len(issue_work([bot, _rest_comment(3, "hi", assoc="NONE")], False).pending) == 1


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
    w = pr_work(pr, True)
    keys = {p.key: p for p in w.pending}
    assert set(keys) == {"issue:1", "review:3", "thread:22"}
    assert keys["thread:22"].reply_target == 20  # replies go to the thread root


def test_pr_work_respects_reply_markers():
    pr = _pr(comments=[_node(1, "please rename"), _node(2, sign("ok", "", reply_to=["issue:1"]))])
    assert pr_work(pr, True) is None


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
    out = asyncio.run(make_guard(lambda t, x: events.append(t))({"tool_input": {"command": cmd}}, None, None))
    assert (out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny") is blocked


# -- auth ----------------------------------------------------------------------------

def test_password_hashing():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) and not verify_password("wrong", h)


def test_sessions_and_rate_limit(tmp_path):
    auth = Auth(Store(tmp_path))
    auth.set_password("supersecret1")
    cookie = auth.new_session()
    assert auth.valid_session(cookie) and not auth.valid_session(cookie + "x")
    auth.set_password("supersecret2")  # rotates secret
    assert not auth.valid_session(cookie)
    for _ in range(5):
        assert not auth.check_password("1.2.3.4", "nope")
    assert not auth.check_password("1.2.3.4", "supersecret2")  # locked out
    assert auth.check_password("5.6.7.8", "supersecret2")


def test_api_requires_auth(tmp_path):
    app = create_app(tmp_path)
    app.state.auth.set_password("supersecret1")
    c = TestClient(app)
    assert c.get("/api/state").status_code == 401
    assert c.get("/", follow_redirects=False).status_code == 303
    h = {"X-Bugsbot": "1"}
    assert c.post("/api/login", json={"password": "wrong"}, headers=h).status_code == 401
    assert c.post("/api/login", json={"password": "supersecret1"}).status_code == 403  # CSRF header missing
    assert c.post("/api/login", json={"password": "supersecret1"}, headers=h).status_code == 200
    state = c.get("/api/state").json()
    assert "admin_password_hash" not in state["settings"] and "session_secret" not in state["settings"]
    assert c.put("/api/settings", json={"session_secret": "x"}, headers=h).status_code == 400
    c.put("/api/settings", json={"anthropic_api_key": "sk-test", "poll_interval_seconds": 30}, headers=h)
    s = c.get("/api/state").json()["settings"]
    assert s["has_anthropic_api_key"] and "anthropic_api_key" not in s and s["poll_interval_seconds"] == 30
    assert c.post("/api/labels", json={"name": "ai"}, headers=h).json() == ["ai"]

    token = c.post("/api/auth/token", headers=h).json()["token"]
    anon = TestClient(app)
    assert anon.get("/api/state", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert anon.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_login_link_single_use(tmp_path):
    app = create_app(tmp_path)
    auth = app.state.auth
    auth.ensure_password()
    t = auth.mint_login_link()
    c = TestClient(app)
    assert c.get(f"/auth/link?t={t}", follow_redirects=False).headers["location"] == "/"
    assert c.get("/api/state").status_code == 200
    assert TestClient(app).get(f"/auth/link?t={t}", follow_redirects=False).headers["location"] == "/login"
