"""Which bugsbot tools a run gets, in a form a tool server in another process can rebuild.

An SDK-driven Claude run builds its tools in-process; agents bugsbot runs as separate
programs (Cursor's CLI, Claude Remote Control) get the same tools from
`python -m bugsbot.agent.mcp_stdio --spec FILE`, which reads a spec written by `write`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bugsbot.agent.tools import PR_OPENED, REPLIED, ToolKit
from bugsbot.github import GitHub
from bugsbot.store import Store
from bugsbot.work import PendingComment
from bugsbot.workspace import Workspace


@dataclass
class ToolSpec:
    kind: str                 # 'issue' | 'pr' | 'worker'
    repo: str
    cwd: str                  # the git checkout pushes come from
    base_branch: str          # target of new PRs
    protected: list[str]      # branches push_branch refuses
    number: int | None = None                  # the issue/PR the run is for (None for workers)
    pending: list[PendingComment] = field(default_factory=list)
    pr_labels: list[str] | None = None         # labels for opened PRs; None lets the agent choose

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))
        path.chmod(0o600)
        return path

    @classmethod
    def read(cls, path: Path) -> "ToolSpec":
        data = json.loads(path.read_text())
        data["pending"] = [PendingComment(**p) for p in data["pending"]]
        return cls(**data)


def record_opened_pr(store: Store, repo: str, issue_number: int, pr: dict[str, Any], created: bool,
                     labels: list[str]) -> None:
    """Track a PR opened for an issue so the harness watches it, and link the two."""
    store.upsert_item(repo, pr["number"], "pr", pr["title"], pr["html_url"], labels, "")
    pr_item = store.item_by_number(repo, pr["number"])
    if pr_item and created:
        # A fresh PR has nothing to respond to until humans comment.
        store.update_item(pr_item["id"], status="idle", status_detail="opened by bugsbot")
    store.link_items(repo, issue_number, pr["number"])


def build_tools(spec: ToolSpec, gh: GitHub, store: Store, run_id: int, signature: str) -> list[Any]:
    kit = ToolKit(gh=gh, repo=spec.repo, signature=signature,
                  workspace=Workspace.at(Path(spec.cwd), spec.repo, gh.token),
                  protected=set(spec.protected), base_branch=spec.base_branch,
                  log=lambda type_, text: store.add_event(run_id, type_, text))
    if spec.kind == "worker":
        return [kit.push_branch(), kit.open_pull_request(labels=None), kit.comment()]
    assert spec.number is not None
    tools = [kit.comment(spec.number), kit.reply_to_comment(spec.number, spec.pending), kit.push_branch()]
    if spec.kind == "issue":
        number = spec.number

        def on_opened(pr: dict[str, Any], created: bool, labels: list[str]) -> None:
            record_opened_pr(store, spec.repo, number, pr, created, labels)
        tools.append(kit.open_pull_request(labels=spec.pr_labels, resolves=number, on_opened=on_opened))
    return tools


def replied_keys(store: Store, run_id: int) -> set[str]:
    return {e["text"] for e in store.events(run_id) if e["type"] == REPLIED}


def opened_prs(store: Store, run_id: int) -> list[int]:
    return sorted({int(e["text"]) for e in store.events(run_id) if e["type"] == PR_OPENED})
