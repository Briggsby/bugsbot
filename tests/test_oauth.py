from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from bugsbot import oauth as oauth_mod
from bugsbot.app import create_app

H = {"X-Bugsbot": "1"}


def fake_github(login: str, orgs: dict[str, str] | None = None):
    """MockTransport standing in for github.com and api.github.com."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == oauth_mod.TOKEN_URL:
            form = parse_qs(request.content.decode())
            if form.get("code") != ["good-code"]:
                return httpx.Response(200, json={"error": "bad_verification_code", "error_description": "bad code"})
            assert form["client_secret"] == ["s3cret"]
            return httpx.Response(200, json={"access_token": "user-token"})
        assert request.headers["authorization"] == "Bearer user-token"
        if url.endswith("/user"):
            return httpx.Response(200, json={"login": login})
        if "/user/memberships/orgs/" in url:
            org = url.rsplit("/", 1)[1]
            state = (orgs or {}).get(org)
            return httpx.Response(200, json={"state": state}) if state else httpx.Response(404, json={})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = create_app(tmp_path, public_url="https://bugsbot.example.com")
    app.state.auth.set_password("supersecret1")
    c = TestClient(app)
    c.post("/api/login", json={"password": "supersecret1"}, headers=H)
    c.put("/api/settings", json={"github_oauth_client_id": "cid", "github_oauth_client_secret": "s3cret",
                                 "oauth_allowed_users": "Alice", "oauth_allowed_orgs": "acme"}, headers=H)
    return app


def sign_in(app, monkeypatch, transport, code="good-code"):
    real = httpx.AsyncClient
    monkeypatch.setattr(oauth_mod.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    c = TestClient(app, base_url="https://testserver")
    r = c.get("/auth/github", follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == ["https://bugsbot.example.com/auth/github/callback"]
    assert q["client_id"] == ["cid"] and q["scope"] == ["read:org"]
    r = c.get(f"/auth/github/callback?code={code}&state={q['state'][0]}", follow_redirects=False)
    return c, r


def test_options(app):
    assert TestClient(app).get("/api/auth/options").json() == {"password": True, "github": True}


def test_allowed_user_signs_in(app, monkeypatch):
    c, r = sign_in(app, monkeypatch, fake_github("alice"))
    assert r.status_code == 200
    assert c.get("/api/state").json()["viewer"] == "alice"


def test_org_member_signs_in(app, monkeypatch):
    c, r = sign_in(app, monkeypatch, fake_github("carol", {"acme": "active"}))
    assert r.status_code == 200 and c.get("/api/state").status_code == 200


def test_pending_org_member_refused(app, monkeypatch):
    c, r = sign_in(app, monkeypatch, fake_github("dave", {"acme": "pending"}))
    assert r.status_code == 403 and "not allowed" in r.text
    assert c.get("/api/state").status_code == 401


def test_bad_code_refused(app, monkeypatch):
    c, r = sign_in(app, monkeypatch, fake_github("alice"), code="evil")
    assert r.status_code == 403 and c.get("/api/state").status_code == 401


def test_state_mismatch_refused(app):
    c = TestClient(app, base_url="https://testserver")
    c.get("/auth/github", follow_redirects=False)
    r = c.get("/auth/github/callback?code=good-code&state=forged", follow_redirects=False)
    assert r.status_code == 403 and c.get("/api/state").status_code == 401


def test_password_toggle(tmp_path):
    app = create_app(tmp_path)
    app.state.auth.set_password("supersecret1")
    c = TestClient(app)
    c.post("/api/login", json={"password": "supersecret1"}, headers=H)
    # Refuses to lock you out when GitHub sign-in isn't configured.
    assert c.put("/api/settings", json={"password_login_enabled": False}, headers=H).status_code == 400
    c.put("/api/settings", json={"github_oauth_client_id": "cid", "github_oauth_client_secret": "x",
                                 "password_login_enabled": False}, headers=H)
    assert TestClient(app).post("/api/login", json={"password": "supersecret1"}, headers=H).status_code == 403
    assert TestClient(app).get("/api/auth/options").json() == {"password": False, "github": True}
