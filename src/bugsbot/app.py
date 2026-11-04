"""FastAPI app: JSON API + the single-page management UI."""

from __future__ import annotations

import hmac
import html
import re
import subprocess
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from bugsbot import providers
from bugsbot.auth import COOKIE, LINK_USER, SESSION_TTL, Auth
from bugsbot.github import GitHub, GitHubError, resolve_token
from bugsbot.harness import Harness
from bugsbot.oauth import GITHUB_SESSION_TTL, STATE_COOKIE, GitHubOAuth, OAuthError
from bugsbot.store import DEFAULT_SETTINGS, INTERNAL_KEYS, SECRET_KEYS, Store
from bugsbot.workers import WorkerManager

PUBLIC_PATHS = {"/login", "/auth/link", "/api/auth/options", "/auth/github", "/auth/github/callback"}

# Navigating to "/" from a page we served makes the next request same-site, so the
# SameSite=Strict session cookie is sent even when sign-in started on github.com.
BOUNCE = '<!doctype html><meta charset="utf-8"><title>bugsbot</title><script>location.replace("/")</script>'
ERROR_PAGE = ('<!doctype html><meta charset="utf-8"><title>bugsbot</title>'
              '<body style="font-family:sans-serif;padding:40px"><h2>Sign-in failed</h2><p>{msg}</p>'
              '<p><a href="/login">Back to sign-in</a></p></body>')

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")


def _logins(value: str | list[str]) -> list[str]:
    items = value.split(",") if isinstance(value, str) else value
    out = [v.strip().lstrip("@") for v in items if v.strip()]
    if bad := [v for v in out if not LOGIN_RE.match(v)]:
        raise HTTPException(400, f"not valid GitHub user or org names: {', '.join(bad)}")
    unique = {v.lower(): v for v in reversed(out)}  # logins are case-insensitive; keep the first spelling
    return sorted(unique.values(), key=str.lower)


class _Overlay:
    """A Store view with unsaved setting values on top, for checking settings before saving them."""

    def __init__(self, store: Store, values: dict[str, Any]):
        self._store, self._values = store, values

    def settings(self) -> dict[str, Any]:
        return {**self._store.settings(), **self._values}

    def get_setting(self, key: str) -> Any:
        return self.settings()[key]


def _page(name: str) -> str:
    return resources.files("bugsbot").joinpath(f"static/{name}").read_text()


def create_app(home: Path, public_url: str | None = None) -> FastAPI:
    store = Store(home)
    auth = Auth(store)
    oauth = GitHubOAuth(store)
    harness = Harness(store, home)
    workers = WorkerManager(store, home)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        workers.recover()  # before harness.start() marks runs left 'running' as interrupted
        harness.start()
        yield
        await workers.stop()
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

    def set_session(request: Request, response: Response, user: str, ttl: int = SESSION_TTL) -> None:
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
        return {"github": oauth.configured}

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
        user = auth.session_user(request.cookies.get(COOKIE)) or LINK_USER
        auth.sign_out_everyone()
        set_session(request, response, user=user)  # keep the caller signed in
        return {"ok": True}

    @app.get("/auth/link")
    async def login_link(request: Request, t: str = "") -> Response:
        if not auth.redeem_login_link(t):
            return RedirectResponse("/login", status_code=303)
        response = HTMLResponse(BOUNCE)
        set_session(request, response, user=LINK_USER)
        return response

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE)
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
            "repo_trust": store.all_repo_trust(),
            "labels": store.labels(),
            "status": {**harness.status, "running": len(harness.tasks)},
            "workers": workers.describe(),
            "providers": [{"name": p.name, "label": p.label, "worker_label": p.worker_label,
                           "worker_needs_repo": p.worker_needs_repo, "worker_needs_task": p.worker_needs_task}
                          for p in providers.PROVIDERS.values()],
            "home": str(home),
        }

    @app.put("/api/settings")
    async def put_settings(values: dict[str, Any] = Body(...)) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS or key in INTERNAL_KEYS:
                raise HTTPException(400, f"unknown setting {key}")
            if key in SECRET_KEYS:
                value = str(value).strip()
                if not value:
                    continue  # blank secret field means "leave unchanged"
            default = DEFAULT_SETTINGS[key]
            try:
                clean[key] = type(default)(value) if not isinstance(default, bool) else bool(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid value for {key}")
        if "agent_provider" in clean and clean["agent_provider"] not in providers.PROVIDERS:
            raise HTTPException(400, f"unknown agent provider {clean['agent_provider']!r}")
        # Check API keys being saved (and the Anthropic key when the model it must work with changes).
        merged = {**store.settings(), **clean}
        for provider in providers.PROVIDERS.values():
            key = provider.credential_setting
            if key in clean or (key == "anthropic_api_key" and "model" in clean and merged[key]):
                store_view = _Overlay(store, clean)
                result = await provider.check_credential(store_view, merged[key])
                if result["ok"] is False:
                    raise HTTPException(400, f"{provider.label} API key not saved: {result['detail']}")
        store.update_settings(clean)
        if "github_token" in clean:
            harness.status["github_user"] = None
        harness.poll_now()
        return public_settings()

    # -- credentials -----------------------------------------------------------

    async def check_github(configured: str) -> dict[str, Any]:
        token = resolve_token(configured)
        source = "token" if configured else "gh CLI login"
        if not token:
            return {"ok": False, "detail": "not set, and the gh CLI on this host isn't logged in"}
        gh = GitHub(token)
        try:
            login = (await gh.viewer())["login"]
        except (GitHubError, httpx.HTTPError) as e:
            return {"ok": False, "detail": f"GitHub rejected the {source}: {str(e)[:200]}"}
        finally:
            await gh.close()
        return {"ok": True, "detail": f"signed in as @{login} (via {source})"}

    def credential_checks() -> dict[str, Any]:
        """Settings key -> async check(value) for every credential on the Credentials card."""
        checks: dict[str, Any] = {p.credential_setting: (lambda v, p=p: p.check_credential(store, v))
                                  for p in providers.PROVIDERS.values()}
        checks["github_token"] = check_github
        return checks

    @app.get("/api/credentials")
    async def credentials_status() -> dict[str, Any]:
        s = store.settings()
        return {key: await check(s[key]) for key, check in credential_checks().items()}

    @app.post("/api/credentials")
    async def save_credentials(values: dict[str, str] = Body(...)) -> dict[str, Any]:
        """Check each credential provided; save the ones that work. Blank means keep the stored value."""
        s = store.settings()
        out: dict[str, Any] = {}
        for key, check in credential_checks().items():
            value = str(values.get(key) or "").strip()
            result = await check(value or s[key])
            if value:
                result["saved"] = bool(result["ok"])
                if result["ok"]:
                    store.update_settings({key: value})
                else:
                    result["detail"] = f"not saved: {result['detail']}"
            out[key] = result
        if out["github_token"].get("saved"):
            harness.status["github_user"] = None
            harness.poll_now()
        return out

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

    @app.put("/api/repos/{owner}/{name}/trust")
    async def set_repo_trust(owner: str, name: str, users: str | list[str] = Body(""),
                             orgs: str | list[str] = Body("")) -> dict[str, list[str]]:
        repo = f"{owner}/{name}"
        if repo not in store.repos():
            raise HTTPException(404, "not a tracked repository")
        store.set_repo_trust(repo, _logins(users), _logins(orgs))
        harness.poll_now()
        return store.repo_trust(repo)

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
        if (worker := workers.get(run_id)) is not None:
            worker.stop()
            return {"cancelled": True}
        return {"cancelled": harness.cancel_run(run_id)}

    # -- remote workers --------------------------------------------------------

    def worker_provider(name: str):
        if name not in providers.PROVIDERS:
            raise HTTPException(400, f"unknown provider {name!r}")
        return providers.PROVIDERS[name]

    @app.get("/api/workers/account")
    async def worker_account(provider: str = "claude") -> dict[str, Any]:
        return await worker_provider(provider).worker_readiness(store, home)

    @app.post("/api/workers")
    async def open_worker(provider: str = Body("claude"), repo: str = Body(""), branch: str = Body(""),
                          name: str = Body(""), task: str = Body("")) -> dict[str, int]:
        p = worker_provider(provider)
        repo, branch, task = repo.strip(), branch.strip(), task.strip()
        if repo and repo not in store.repos():
            raise HTTPException(400, "repo must be one of the tracked repositories")
        if p.worker_needs_repo and not repo:
            raise HTTPException(400, f"a {p.worker_label} needs a repository")
        if p.worker_needs_task and not task:
            raise HTTPException(400, f"a {p.worker_label} needs a first message describing the task")
        if branch and not repo:
            raise HTTPException(400, "a branch needs a repository")
        if branch and subprocess.run(["git", "check-ref-format", "--branch", branch],
                                     capture_output=True).returncode != 0:
            raise HTTPException(400, f"'{branch}' isn't a valid git branch name")
        ready = await p.worker_readiness(store, home)
        if not ready["ok"]:
            raise HTTPException(400, f"Can't start a {p.worker_label}: {ready['detail']}")
        return {"run_id": workers.open(p.name, repo, branch, name, task)}

    @app.post("/api/workers/{run_id}/stop")
    async def stop_worker(run_id: int) -> dict[str, bool]:
        worker = workers.get(run_id)
        if worker is None:
            raise HTTPException(404, "worker is not running")
        worker.stop()
        return {"ok": True}

    @app.post("/api/poll")
    async def poll() -> dict[str, str]:
        harness.poll_now()
        return {"ok": "polling"}

    return app
