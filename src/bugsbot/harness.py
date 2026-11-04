"""Polling loop and agent scheduler."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from pathlib import Path
from typing import Any

from bugsbot.agent import AgentRun, RunContext
from bugsbot.github import GitHub, GitHubError, resolve_token
from bugsbot.store import Store
from bugsbot.work import Work, issue_work, pr_work

log = logging.getLogger("bugsbot")


class Harness:
    def __init__(self, store: Store, home: Path):
        self.store = store
        self.home = home
        self.tasks: dict[int, tuple[int, asyncio.Task]] = {}  # item_id -> (run_id, task)
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self.status: dict[str, Any] = {
            "last_poll_started": None, "last_poll_finished": None, "next_poll_at": None,
            "errors": [], "github_user": None,
        }

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self.store.mark_interrupted_runs()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
        for _, task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*(t for _, t in self.tasks.values()), return_exceptions=True)

    def poll_now(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            settings = self.store.settings()
            if settings["polling_enabled"]:
                try:
                    await self.poll_once()
                except Exception as e:  # never let the loop die
                    log.exception("poll failed")
                    self.status["errors"] = [f"poll failed: {e}"]
            interval = max(15, int(settings["poll_interval_seconds"]))
            self.status["next_poll_at"] = time.time() + interval if settings["polling_enabled"] else None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def github(self) -> GitHub:
        return GitHub(resolve_token(self.store.get_setting("github_token")))

    # -- polling ---------------------------------------------------------------

    async def poll_once(self) -> None:
        self.status["last_poll_started"] = time.time()
        errors: list[str] = []
        settings = self.store.settings()
        repos, labels = self.store.repos(), self.store.labels()
        gh = self.github()
        try:
            if not self.status["github_user"]:
                self.status["github_user"] = (await gh.viewer())["login"]
            for repo in repos:
                try:
                    await self._poll_repo(gh, repo, labels, settings)
                except GitHubError as e:
                    errors.append(f"{repo}: {e}")
        finally:
            await gh.close()
            self.status["errors"] = errors
            self.status["last_poll_finished"] = time.time()

    async def _poll_repo(self, gh: GitHub, repo: str, labels: list[str], settings: dict[str, Any]) -> None:
        found: dict[int, dict[str, Any]] = {}
        for label in labels:
            for raw in await gh.labelled_open(repo, label):
                found[raw["number"]] = raw
        self.store.deactivate_missing(repo, set(found))
        label_set = set(labels)

        for number, raw in sorted(found.items()):
            kind = "pr" if "pull_request" in raw else "issue"
            item_labels = [l["name"] for l in raw["labels"] if l["name"] in label_set]
            item = self.store.upsert_item(repo, number, kind, raw["title"], raw["html_url"],
                                          item_labels, raw["updated_at"])
            if item["id"] in self.tasks:
                continue
            if item["gh_updated_at"] == raw["updated_at"] and item["status"] not in ("new", "pending"):
                continue  # nothing changed on GitHub since we last evaluated it
            await self._evaluate(gh, item, raw["updated_at"], settings)

    async def _evaluate(self, gh: GitHub, item: dict[str, Any], updated_at: str,
                        settings: dict[str, Any]) -> None:
        trusted_only = bool(settings["trusted_commenters_only"])
        if item["kind"] == "issue":
            work = issue_work(await gh.issue_comments(item["repo"], item["number"]), trusted_only)
        else:
            work = pr_work(await gh.pr_context(item["repo"], item["number"]), trusted_only)
        fields: dict[str, Any] = {"last_checked_at": time.time()}

        if work is None:
            fields.update(status="idle", status_detail="nothing to respond to", gh_updated_at=updated_at)
        elif work.trigger_key == item["last_trigger_key"] and item["last_run_status"] not in ("", "interrupted"):
            # Already ran for exactly this trigger; don't loop on it.
            ok = item["last_run_status"] == "succeeded"
            fields.update(status="idle" if ok else "failed", gh_updated_at=updated_at,
                          status_detail=("already handled" if ok else "last run failed") +
                          "; waiting for new activity or a manual retry")
        elif len(self.tasks) >= int(settings["max_concurrent_agents"]):
            # Leave gh_updated_at stale so we re-evaluate on the next poll.
            fields.update(status="pending", status_detail=f"queued: {work.trigger}")
        else:
            fields.update(gh_updated_at=updated_at)
            self.store.update_item(item["id"], **fields)
            self._start_run(item, work)
            return
        self.store.update_item(item["id"], **fields)

    # -- runs ------------------------------------------------------------------

    def _start_run(self, item: dict[str, Any], work: Work) -> int:
        run_id = self.store.start_run(item, work.trigger, work.trigger_key)
        self.store.update_item(item["id"], status="running", status_detail=work.trigger,
                               last_trigger_key=work.trigger_key)
        task = asyncio.create_task(self._execute(run_id, item, work))
        self.tasks[item["id"]] = (run_id, task)
        return run_id

    async def _execute(self, run_id: int, item: dict[str, Any], work: Work) -> None:
        gh = self.github()
        status, summary, error, turns, cost = "failed", "", "", None, None
        try:
            ctx = RunContext(store=self.store, gh=gh, home=self.home, run_id=run_id, item=item,
                             work=work, tracked_labels=self.store.labels())
            status, summary, turns, cost = await AgentRun(ctx).run()
        except asyncio.CancelledError:
            status, error = "cancelled", "cancelled from the web UI"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            self.store.add_event(run_id, "error", traceback.format_exc())
        finally:
            await gh.close()
            self.store.finish_run(run_id, status, summary=summary, error=error, num_turns=turns, cost_usd=cost)
            item_status = {"succeeded": "idle", "cancelled": "failed"}.get(status, "failed")
            # Clear gh_updated_at so the next poll re-checks the item (our own comments changed it).
            self.store.update_item(item["id"], status=item_status, last_run_status=status,
                                   status_detail=(error or summary)[:300], gh_updated_at="")
            self.tasks.pop(item["id"], None)
            self.poll_now()  # a slot freed up; pick up queued work promptly

    def cancel_run(self, run_id: int) -> bool:
        for rid, task in self.tasks.values():
            if rid == run_id:
                task.cancel()
                return True
        return False

    def retry_item(self, item_id: int) -> None:
        self.store.update_item(item_id, last_trigger_key="", last_run_status="", gh_updated_at="",
                               status="pending", status_detail="manual retry requested")
        self.poll_now()
