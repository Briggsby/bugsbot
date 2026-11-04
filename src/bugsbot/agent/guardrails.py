"""Install bugsbot's rules into a checkout for a plain Claude Code process.

SDK-driven runs get the guard hook, tools and system prompt through ClaudeAgentOptions.
A `claude remote-control` worker can't, so the same things go into project-local
Claude Code config in its checkout, hidden from git so the agent never commits them:

- .claude/settings.local.json: the PreToolUse guard hook; auto-enables the bugsbot MCP server
- .mcp.json: the signed GitHub tools (only when the worker has a repository)
- CLAUDE.local.md: the hard rules from the system prompt
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from bugsbot.agent.policy import SYSTEM_APPEND

MARKER = "<!-- bugsbot rules -->"
FILES = [".claude/settings.local.json", ".mcp.json", "CLAUDE.local.md"]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _hide_from_git(cwd: Path) -> None:
    if not (cwd / ".git").exists():
        return
    exclude = cwd / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    missing = [f for f in FILES if f not in existing.splitlines()]
    if missing:
        exclude.write_text(existing.rstrip("\n") + "\n" + "\n".join(missing) + "\n")
    # Files the repo already tracks: keep our edits out of the agent's commits.
    for f in FILES:
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", f], cwd=cwd, capture_output=True)
        if tracked.returncode == 0:
            subprocess.run(["git", "update-index", "--skip-worktree", f], cwd=cwd, check=False)


def install(cwd: Path, home: Path, run_id: int, repo: str | None, extra_rules: str = "") -> None:
    py = sys.executable
    common = ["--home", str(home), "--run-id", str(run_id)]

    settings_path = cwd / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _load(settings_path)
    hook_cmd = shlex.join([py, "-m", "bugsbot.agent.hook", *common])
    hooks = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not any(h.get("hooks", [{}])[0].get("command") == hook_cmd for h in hooks):
        hooks.append({"matcher": "Bash", "hooks": [{"type": "command", "command": hook_cmd}]})

    if repo:
        mcp_path = cwd / ".mcp.json"
        mcp = _load(mcp_path)
        mcp.setdefault("mcpServers", {})["bugsbot"] = {
            "command": py,
            "args": ["-m", "bugsbot.agent.mcp_stdio", *common, "--repo", repo, "--cwd", str(cwd)],
        }
        mcp_path.write_text(json.dumps(mcp, indent=2) + "\n")
        enabled = settings.setdefault("enabledMcpjsonServers", [])
        if "bugsbot" not in enabled:
            enabled.append("bugsbot")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    rules_path = cwd / "CLAUDE.local.md"
    rules = rules_path.read_text() if rules_path.exists() else ""
    if MARKER not in rules:
        rules_path.write_text(f"{rules}\n{MARKER}\n{SYSTEM_APPEND.strip()}\n{extra_rules.strip()}\n".lstrip())
    _hide_from_git(cwd)
