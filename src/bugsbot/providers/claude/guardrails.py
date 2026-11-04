"""Install bugsbot's rules into a checkout for a plain Claude Code process (a Remote Control worker).

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
import sys
from pathlib import Path

from bugsbot.agent.policy import SYSTEM_APPEND
from bugsbot.workspace import hide_from_git

MARKER = "<!-- bugsbot rules -->"
FILES = [".claude/settings.local.json", ".mcp.json", "CLAUDE.local.md"]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def install(cwd: Path, home: Path, run_id: int, tool_spec: Path | None, extra_rules: str = "") -> None:
    """`tool_spec`: a ToolSpec file for the bugsbot tools, or None for no tools (no repository)."""
    py = sys.executable
    common = ["--home", str(home), "--run-id", str(run_id)]

    settings_path = cwd / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _load(settings_path)
    hook_cmd = shlex.join([py, "-m", "bugsbot.agent.hook", *common, "--provider", "claude"])
    hooks = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not any(h.get("hooks", [{}])[0].get("command") == hook_cmd for h in hooks):
        hooks.append({"matcher": "Bash", "hooks": [{"type": "command", "command": hook_cmd}]})

    if tool_spec:
        mcp_path = cwd / ".mcp.json"
        mcp = _load(mcp_path)
        mcp.setdefault("mcpServers", {})["bugsbot"] = {
            "command": py,
            "args": ["-m", "bugsbot.agent.mcp_stdio", *common, "--spec", str(tool_spec)],
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
    hide_from_git(cwd, FILES)
