"""SQLite-backed persistent state: settings, tracked repos/labels, items and agent runs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    full_name TEXT PRIMARY KEY,
    added_at REAL NOT NULL,
    trusted_users TEXT NOT NULL DEFAULT '[]',  -- GitHub logins; with trusted_orgs, empty = no restriction
    trusted_orgs TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS labels (
    name TEXT PRIMARY KEY,
    added_at REAL NOT NULL
);
-- An issue or PR that carries (or carried) a tracked label.
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- 'issue' | 'pr'
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,  -- still open and labelled
    status TEXT NOT NULL DEFAULT 'new', -- new|idle|pending|running|failed|done
    status_detail TEXT NOT NULL DEFAULT '',
    gh_updated_at TEXT NOT NULL DEFAULT '',
    last_checked_at REAL,
    last_trigger_key TEXT NOT NULL DEFAULT '',
    last_run_status TEXT NOT NULL DEFAULT '',
    linked_numbers TEXT NOT NULL DEFAULT '[]', -- PRs opened for an issue / issue a PR came from
    first_seen_at REAL NOT NULL,
    UNIQUE(repo, number)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id),  -- NULL for remote workers
    repo TEXT NOT NULL,                     -- '' for workers without a repo
    number INTEGER NOT NULL,                -- 0 for workers
    kind TEXT NOT NULL,                     -- 'issue' | 'pr' | 'worker'
    trigger TEXT NOT NULL DEFAULT '',
    trigger_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running', -- running|succeeded|failed|cancelled|interrupted
    started_at REAL NOT NULL,
    ended_at REAL,
    num_turns INTEGER,
    cost_usd REAL,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    worker_pid INTEGER                      -- remote worker process, for cleanup after a crash
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL
);
-- Single-use sign-in links minted from the server's terminal (stored hashed).
CREATE TABLE IF NOT EXISTS login_links (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id, id);
CREATE INDEX IF NOT EXISTS runs_item ON runs(item_id, id);
"""

DEFAULT_SIGNATURE = (
    "🤖 *This was written by [bugsbot](https://github.com/Briggsby/bugsbot), an AI agent. "
    "It may be wrong — please review carefully.*"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "anthropic_api_key": "",
    "github_token": "",
    "model": "claude-opus-5-5",
    "effort": "high",
    "poll_interval_seconds": 60,
    "max_concurrent_agents": 2,
    "max_turns_per_run": 200,
    "max_budget_usd_per_run": 10.0,
    "trusted_commenters_only": True,
    "polling_enabled": True,
    "git_author_name": "bugsbot",
    "git_author_email": "bugsbot@users.noreply.github.com",
    "signature": DEFAULT_SIGNATURE,
    # Web UI sign-in (GitHub OAuth)
    "github_oauth_client_id": "",
    "github_oauth_client_secret": "",
    "oauth_allowed_users": "",          # comma-separated GitHub logins
    "oauth_allowed_orgs": "",           # comma-separated org logins
    "oauth_allow_repo_collaborators": False,  # anyone with write access to a tracked repo
    # Internal: never returned by or settable through the settings API.
    "session_secret": "",
    "api_token_hash": "",
}

SECRET_KEYS = {"anthropic_api_key", "github_token", "github_oauth_client_secret"}
INTERNAL_KEYS = {"session_secret", "api_token_hash"}


def default_home() -> Path:
    return Path(os.environ.get("BUGSBOT_HOME", Path.home() / ".bugsbot"))


class Store:
    """Thin thread-safe wrapper around one SQLite connection."""

    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        self.path = home / "bugsbot.db"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        for migration in (  # columns added after the first release
            "ALTER TABLE runs ADD COLUMN worker_pid INTEGER",
            "ALTER TABLE repos ADD COLUMN trusted_users TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE repos ADD COLUMN trusted_orgs TEXT NOT NULL DEFAULT '[]'",
        ):
            try:
                self._db.execute(migration)
            except sqlite3.OperationalError:
                pass  # already applied
        os.chmod(self.path, 0o600)

    def _q(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _x(self, sql: str, params: tuple | dict = ()) -> int:
        with self._lock:
            return self._db.execute(sql, params).lastrowid or 0

    # -- settings -------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for row in self._q("SELECT key, value FROM settings"):
            if row["key"] in DEFAULT_SETTINGS:  # ignore keys from older versions
                out[row["key"]] = json.loads(row["value"])
        return out

    def get_setting(self, key: str) -> Any:
        return self.settings()[key]

    def update_settings(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                raise KeyError(key)
            self._x(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # -- login links ------------------------------------------------------------

    def add_login_link(self, token_hash: str, expires_at: float) -> None:
        self._x("DELETE FROM login_links WHERE expires_at < ?", (time.time(),))
        self._x("INSERT INTO login_links VALUES (?, ?)", (token_hash, expires_at))

    def consume_login_link(self, token_hash: str) -> bool:
        with self._lock:
            rows = self._q("SELECT expires_at FROM login_links WHERE token_hash = ?", (token_hash,))
            self._x("DELETE FROM login_links WHERE token_hash = ?", (token_hash,))
        return bool(rows) and rows[0]["expires_at"] > time.time()

    # -- repos & labels -------------------------------------------------------

    def repos(self) -> list[str]:
        return [r["full_name"] for r in self._q("SELECT full_name FROM repos ORDER BY full_name")]

    def add_repo(self, full_name: str) -> None:
        self._x("INSERT OR IGNORE INTO repos(full_name, added_at) VALUES (?, ?)", (full_name, time.time()))

    def repo_trust(self, full_name: str) -> dict[str, list[str]]:
        rows = self._q("SELECT trusted_users, trusted_orgs FROM repos WHERE full_name = ?", (full_name,))
        if not rows:
            return {"users": [], "orgs": []}
        return {"users": json.loads(rows[0]["trusted_users"]), "orgs": json.loads(rows[0]["trusted_orgs"])}

    def all_repo_trust(self) -> dict[str, dict[str, list[str]]]:
        return {r: self.repo_trust(r) for r in self.repos()}

    def set_repo_trust(self, full_name: str, users: list[str], orgs: list[str]) -> None:
        self._x("UPDATE repos SET trusted_users = ?, trusted_orgs = ? WHERE full_name = ?",
                (json.dumps(users), json.dumps(orgs), full_name))
        # Who counts as trusted changed, so every item needs re-evaluating on the next poll.
        self._x("UPDATE items SET gh_updated_at = '' WHERE repo = ?", (full_name,))

    def remove_repo(self, full_name: str) -> None:
        self._x("DELETE FROM repos WHERE full_name = ?", (full_name,))
        self._x("UPDATE items SET active = 0 WHERE repo = ?", (full_name,))

    def labels(self) -> list[str]:
        return [r["name"] for r in self._q("SELECT name FROM labels ORDER BY name")]

    def add_label(self, name: str) -> None:
        self._x("INSERT OR IGNORE INTO labels VALUES (?, ?)", (name, time.time()))

    def remove_label(self, name: str) -> None:
        self._x("DELETE FROM labels WHERE name = ?", (name,))

    # -- items ----------------------------------------------------------------

    def upsert_item(self, repo: str, number: int, kind: str, title: str, url: str,
                    labels: list[str], gh_updated_at: str) -> dict[str, Any]:
        self._x(
            "INSERT INTO items(repo, number, kind, title, url, labels, gh_updated_at, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '', ?) "
            "ON CONFLICT(repo, number) DO UPDATE SET title = excluded.title, url = excluded.url, "
            "labels = excluded.labels, kind = excluded.kind, active = 1",
            (repo, number, kind, title, url, json.dumps(labels), time.time()),
        )
        return self.item_by_number(repo, number)  # type: ignore[return-value]

    def item(self, item_id: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM items WHERE id = ?", (item_id,))
        return _decode_item(rows[0]) if rows else None

    def item_by_number(self, repo: str, number: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM items WHERE repo = ? AND number = ?", (repo, number))
        return _decode_item(rows[0]) if rows else None

    def items(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if kind:
            sql += " WHERE kind = ?"
            params = (kind,)
        sql += " ORDER BY active DESC, first_seen_at DESC"
        return [_decode_item(r) for r in self._q(sql, params)]

    def update_item(self, item_id: int, **fields: Any) -> None:
        if "linked_numbers" in fields:
            fields["linked_numbers"] = json.dumps(sorted(set(fields["linked_numbers"])))
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        self._x(f"UPDATE items SET {cols} WHERE id = :id", {**fields, "id": item_id})

    def link_items(self, repo: str, issue_number: int, pr_number: int) -> None:
        for a, b in ((issue_number, pr_number), (pr_number, issue_number)):
            item = self.item_by_number(repo, a)
            if item:
                self.update_item(item["id"], linked_numbers=[*item["linked_numbers"], b])

    def deactivate_missing(self, repo: str, seen_numbers: set[int]) -> None:
        for item in self._q("SELECT id, number FROM items WHERE repo = ? AND active = 1", (repo,)):
            if item["number"] not in seen_numbers:
                self._x("UPDATE items SET active = 0 WHERE id = ?", (item["id"],))

    # -- runs -----------------------------------------------------------------

    def start_run(self, item: dict[str, Any], trigger: str, trigger_key: str) -> int:
        return self._x(
            "INSERT INTO runs(item_id, repo, number, kind, trigger, trigger_key, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item["repo"], item["number"], item["kind"], trigger, trigger_key, time.time()),
        )

    def start_worker(self, repo: str, title: str) -> int:
        return self._x(
            "INSERT INTO runs(item_id, repo, number, kind, trigger, started_at) VALUES (NULL, ?, 0, 'worker', ?, ?)",
            (repo, title, time.time()),
        )

    def set_run_summary(self, run_id: int, summary: str) -> None:
        self._x("UPDATE runs SET summary = ? WHERE id = ?", (summary, run_id))

    def set_run_pid(self, run_id: int, pid: int) -> None:
        self._x("UPDATE runs SET worker_pid = ? WHERE id = ?", (pid, run_id))

    def orphaned_worker_pids(self) -> list[int]:
        rows = self._q("SELECT worker_pid FROM runs WHERE status = 'running' AND worker_pid IS NOT NULL")
        return [r["worker_pid"] for r in rows]

    def finish_run(self, run_id: int, status: str, summary: str = "", error: str = "",
                   num_turns: int | None = None, cost_usd: float | None = None) -> None:
        self._x(
            "UPDATE runs SET status = ?, ended_at = ?, summary = ?, error = ?, "
            "num_turns = COALESCE(?, num_turns), cost_usd = COALESCE(?, cost_usd) WHERE id = ?",
            (status, time.time(), summary, error, num_turns, cost_usd, run_id),
        )

    def run(self, run_id: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _decode_run(rows[0]) if rows else None

    def runs(self, limit: int = 200, item_id: int | None = None) -> list[dict[str, Any]]:
        if item_id is not None:
            rows = self._q("SELECT * FROM runs WHERE item_id = ? ORDER BY id DESC LIMIT ?", (item_id, limit))
        else:
            rows = self._q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [_decode_run(r) for r in rows]

    def add_event(self, run_id: int, type_: str, text: str) -> None:
        self._x("INSERT INTO run_events(run_id, ts, type, text) VALUES (?, ?, ?, ?)",
                (run_id, time.time(), type_, text))

    def events(self, run_id: int, after_id: int = 0) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM run_events WHERE run_id = ? AND id > ? ORDER BY id",
                       (run_id, after_id))

    def mark_interrupted_runs(self) -> None:
        """Runs left 'running' by a previous process can never finish; close them out."""
        self._x("UPDATE runs SET status = 'interrupted', ended_at = ? WHERE status = 'running'",
                (time.time(),))
        self._x("UPDATE items SET status = 'pending', status_detail = 'previous run interrupted' "
                "WHERE status = 'running'")


def _decode_item(row: dict[str, Any]) -> dict[str, Any]:
    row["labels"] = json.loads(row["labels"])
    row["linked_numbers"] = json.loads(row["linked_numbers"])
    row["active"] = bool(row["active"])
    return row


def _decode_run(row: dict[str, Any]) -> dict[str, Any]:
    end = row["ended_at"] or time.time()
    row["duration_seconds"] = round(end - row["started_at"], 1)
    return row
