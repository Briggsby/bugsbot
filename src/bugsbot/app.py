"""FastAPI app: JSON API + the single-page management UI."""

from __future__ import annotations

import hmac
import html
import re
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import COOKIE, SESSION_TTL, Auth
from .chat import ChatManager
from .github import GitHubError
from .harness import Harness
from .oauth import GITHUB_SESSION_TTL, STATE_COOKIE, GitHubOAuth, OAuthError
from .store import DEFAULT_SETTINGS, INTERNAL_KEYS, SECRET_KEYS, Store

PUBLIC_PATHS = {"/login", "/api/login", "/auth/link", "/api/auth/options", "/auth/github", "/auth/github/callback"}

# Navigating to "/" from a page we served makes the next request same-site, so the
# SameSite=Strict session cookie is sent even when sign-in started on github.com.
BOUNCE = '<!doctype html><meta charset="utf-8"><title>bugsbot</title><script>location.replace("/")</script>'
ERROR_PAGE = ('<!doctype html><meta charset="utf-8"><title>bugsbot</title>'
              '<body style="font-family:sans-serif;padding:40px"><h2>Sign-in failed</h2><p>{msg}</p>'
              '<p><a href="/login">Back to sign-in</a></p></body>')

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _page(name: str) -> str:
    return resources.files("bugsbot").joinpath(f"static/{name}").read_text()


def create_app(home: Path, public_url: str | None = None) -> FastAPI:
    store = Store(home)
    auth = Auth(store)
    oauth = GitHubOAuth(store)
    harness = Harness(store, home)
    chats = ChatManager(store, home)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        harness.start()
        yield
        await chats.stop()
        await harness.stop()

    app = FastAPI(title="bugsbot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        # A custom header on writes forces a CORS preflight, which we never grant: blocks CSRF.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-bugsbot") != "1":
            return JSONResponse({"detail": "missing X-Bugsbot header"}, status_code=403)
        path = request.url.path
        authed = (auth.valid_session(request.cookies.get(COOKIE))
                  or auth.valid_bearer(request.headers.get("authorization")))
        if not authed and path not in PUBLIC_PATHS:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def is_secure(request: Request) -> bool:
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

    def set_session(request: Request, response: Response, user: str = "admin",
                    ttl: int = SESSION_TTL) -> None:
        response.set_cookie(COOKIE, auth.new_session(user, ttl), max_age=ttl, httponly=True,
                            secure=is_secure(request), samesite="strict")

    def callback_url(request: Request) -> str:
        base = (public_url or str(request.base_url)).rstrip("/")
        return f"{base}/auth/github/callback"

    # -- auth ------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> str:
        return _page("login.html")

    @app.get("/api/auth/options")
    async def auth_options() -> dict[str, bool]:
        return {"password": bool(store.get_setting("password_login_enabled")) and auth.has_password,
                "github": oauth.configured}

    @app.get("/auth/github")
    async def github_login(request: Request) -> Response:
        if not oauth.configured:
            return HTMLResponse(ERROR_PAGE.format(msg="GitHub sign-in is not configured."), status_code=400)
        url, state = oauth.authorize_url(callback_url(request))
        response = RedirectResponse(url, status_code=303)
        # Lax (not Strict) so the cookie comes back on the top-level redirect from github.com.
        response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=is_secure(request),
                            samesite="lax", path="/auth/github")
        return response

    @app.get("/auth/github/callback")
    async def github_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
        expected = request.cookies.get(STATE_COOKIE)
        try:
            if error:
                raise OAuthError(error)
            if not code or not expected or not hmac.compare_digest(state, expected):
                raise OAuthError("invalid or expired sign-in attempt; please try again")
            login = await oauth.complete(code, callback_url(request))
        except (OAuthError, httpx.HTTPError) as e:
            response: Response = HTMLResponse(ERROR_PAGE.format(msg=html.escape(str(e))), status_code=403)
        else:
            response = HTMLResponse(BOUNCE)
            set_session(request, response, user=f"gh:{login}", ttl=GITHUB_SESSION_TTL)
        response.delete_cookie(STATE_COOKIE, path="/auth/github")
        return response

    @app.post("/api/auth/signout-all")
    async def signout_all(request: Request, response: Response) -> dict[str, bool]:
        user = auth.session_user(request.cookies.get(COOKIE)) or "admin"
        auth.sign_out_everyone()
        set_session(request, response, user=user)  # keep the caller signed in
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request, response: Response, password: str = Body(..., embed=True)) -> dict[str, bool]:
        if not store.get_setting("password_login_enabled"):
            raise HTTPException(403, "password sign-in is disabled; use GitHub")
        client = request.client.host if request.client else "?"
        if auth.rate_limited(client):
            raise HTTPException(429, "too many failed attempts; try again in a few minutes")
        if not auth.check_password(client, password):
            raise HTTPException(401, "wrong password")
        set_session(request, response)
        return {"ok": True}

    @app.get("/auth/link")
    async def login_link(request: Request, t: str = "") -> Response:
        if not auth.redeem_login_link(t):
            return RedirectResponse("/login", status_code=303)
        response = HTMLResponse(BOUNCE)
        set_session(request, response)
        return response

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.post("/api/auth/password")
    async def change_password(request: Request, response: Response, current: str = Body(...),
                              new: str = Body(...)) -> dict[str, bool]:
        client = request.client.host if request.client else "?"
        if not auth.check_password(client, current):
            raise HTTPException(401, "current password is wrong")
        try:
            auth.set_password(new)
        except ValueError as e:
            raise HTTPException(400, str(e))
        set_session(request, response)  # old sessions are invalidated; keep this one signed in
        return {"ok": True}

    @app.post("/api/auth/token")
    async def new_api_token() -> dict[str, str]:
        return {"token": auth.regenerate_api_token()}

    @app.delete("/api/auth/token")
    async def revoke_api_token() -> dict[str, bool]:
        auth.revoke_api_token()
        return {"ok": True}

    # -- UI & state ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _page("index.html")

    def public_settings() -> dict[str, Any]:
        s = store.settings()
        s["has_api_token"] = bool(s["api_token_hash"])
        for key in INTERNAL_KEYS:
            s.pop(key)
        for key in SECRET_KEYS:
            s[f"has_{key}"] = bool(s.pop(key))
        return s

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        user = auth.session_user(request.cookies.get(COOKIE)) or "api-token"
        return {
            "viewer": user.removeprefix("gh:"),
            "oauth_callback_url": callback_url(request),
            "settings": public_settings(),
            "repos": store.repos(),
            "labels": store.labels(),
            "status": {**harness.status, "running": len(harness.tasks)},
            "chats": {rid: {"busy": c.busy} for rid, c in chats.sessions.items()},
            "home": str(home),
        }

    @app.put("/api/settings")
    async def put_settings(values: dict[str, Any] = Body(...)) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS or key in INTERNAL_KEYS:
                raise HTTPException(400, f"unknown setting {key}")
            if key in SECRET_KEYS and not value:
                continue  # blank secret field means "leave unchanged"
            default = DEFAULT_SETTINGS[key]
            try:
                clean[key] = type(default)(value) if not isinstance(default, bool) else bool(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid value for {key}")
        merged = {**store.settings(), **clean}
        if not merged["password_login_enabled"] and not (
                merged["github_oauth_client_id"] and merged["github_oauth_client_secret"]):
            raise HTTPException(400, "configure GitHub sign-in before disabling password sign-in")
        store.update_settings(clean)
        if "github_token" in clean:
            harness.status["github_user"] = None
        harness.poll_now()
        return public_settings()

    @app.delete("/api/secrets/{key}")
    async def clear_secret(key: str) -> dict[str, Any]:
        if key not in SECRET_KEYS:
            raise HTTPException(404)
        store.update_settings({key: ""})
        harness.status["github_user"] = None
        return public_settings()

    @app.get("/api/github/repos")
    async def github_repos() -> list[dict[str, Any]]:
        try:
            gh = harness.github()
        except GitHubError as e:
            raise HTTPException(400, str(e))
        try:
            return await gh.user_repos()
        except GitHubError as e:
            raise HTTPException(502, str(e))
        finally:
            await gh.close()

    @app.post("/api/repos")
    async def add_repo(full_name: str = Body(..., embed=True)) -> list[str]:
        full_name = full_name.strip().removeprefix("https://github.com/").strip("/")
        if not REPO_RE.match(full_name):
            raise HTTPException(400, "expected owner/name")
        try:
            gh = harness.github()
            try:
                info = await gh.repo(full_name)
            finally:
                await gh.close()
        except GitHubError as e:
            raise HTTPException(400, f"cannot access {full_name}: {e}")
        store.add_repo(info["full_name"])
        harness.poll_now()
        return store.repos()

    @app.delete("/api/repos/{owner}/{name}")
    async def remove_repo(owner: str, name: str) -> list[str]:
        store.remove_repo(f"{owner}/{name}")
        return store.repos()

    @app.post("/api/labels")
    async def add_label(name: str = Body(..., embed=True)) -> list[str]:
        name = name.strip()
        if not name or "," in name:
            raise HTTPException(400, "label names must be non-empty and contain no commas")
        store.add_label(name)
        harness.poll_now()
        return store.labels()

    @app.delete("/api/labels/{name}")
    async def remove_label(name: str) -> list[str]:
        store.remove_label(name)
        harness.poll_now()
        return store.labels()

    @app.get("/api/items")
    async def items(kind: str | None = None) -> list[dict[str, Any]]:
        out = store.items(kind)
        for item in out:
            runs = store.runs(limit=1, item_id=item["id"])
            item["last_run"] = runs[0] if runs else None
            item["run_count"] = len(store.runs(limit=10_000, item_id=item["id"]))
        return out

    @app.post("/api/items/{item_id}/retry")
    async def retry(item_id: int) -> dict[str, str]:
        if not store.item(item_id):
            raise HTTPException(404)
        harness.retry_item(item_id)
        return {"ok": "queued"}

    @app.get("/api/runs")
    async def runs(limit: int = 200) -> list[dict[str, Any]]:
        return store.runs(limit=limit)

    @app.get("/api/runs/{run_id}")
    async def run(run_id: int, after: int = 0) -> dict[str, Any]:
        r = store.run(run_id)
        if not r:
            raise HTTPException(404)
        return {"run": r, "events": store.events(run_id, after)}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: int) -> dict[str, bool]:
        if (session := chats.get(run_id)) is not None:
            session.task.cancel()
            return {"cancelled": True}
        return {"cancelled": harness.cancel_run(run_id)}

    # -- chats -----------------------------------------------------------------

    def chat(run_id: int):
        session = chats.get(run_id)
        if session is None:
            raise HTTPException(404, "chat is not open")
        return session

    @app.post("/api/chats")
    async def open_chat(repo: str = Body(""), branch: str = Body(""), message: str = Body("")) -> dict[str, int]:
        repo = repo.strip()
        if repo and repo not in store.repos():
            raise HTTPException(400, "repo must be one of the tracked repositories")
        return {"run_id": chats.open(repo, branch.strip(), message)}

    @app.post("/api/chats/{run_id}/messages")
    async def chat_message(run_id: int, text: str = Body(..., embed=True)) -> dict[str, bool]:
        if not text.strip():
            raise HTTPException(400, "empty message")
        chat(run_id).send(text)
        return {"ok": True}

    @app.post("/api/chats/{run_id}/interrupt")
    async def chat_interrupt(run_id: int) -> dict[str, bool]:
        await chat(run_id).interrupt()
        return {"ok": True}

    @app.post("/api/chats/{run_id}/close")
    async def chat_close(run_id: int) -> dict[str, bool]:
        chat(run_id).close()
        return {"ok": True}

    @app.post("/api/poll")
    async def poll() -> dict[str, str]:
        harness.poll_now()
        return {"ok": "polling"}

    return app
