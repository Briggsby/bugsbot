"""Install bugsbot's tools and shell-command check into a checkout for Cursor's CLI.

The CLI takes no system prompt or hook options on its command line, so they go into
project-local Cursor config, hidden from git so the agent never commits them:

- .cursor/mcp.json: the signed GitHub tools (bugsbot.agent.mcp_stdio)
- .cursor/hooks.json: a beforeShellExecution hook running bugsbot's command check

The rules themselves go at the top of the prompt (see agent.py).
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from bugsbot.workspace import hide_from_git

FILES = [".cursor/mcp.json", ".cursor/hooks.json"]
HOOK_MODULE = "bugsbot.agent.hook"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def install(cwd: Path, home: Path, run_id: int, tool_spec: Path) -> None:
    py = sys.executable
    common = ["--home", str(home), "--run-id", str(run_id)]
    (cwd / ".cursor").mkdir(exist_ok=True)

    mcp_path = cwd / ".cursor" / "mcp.json"
    mcp = _load(mcp_path)
    mcp.setdefault("mcpServers", {})["bugsbot"] = {
        "command": py, "args": ["-m", "bugsbot.agent.mcp_stdio", *common, "--spec", str(tool_spec)],
    }
    mcp_path.write_text(json.dumps(mcp, indent=2) + "\n")

    hooks_path = cwd / ".cursor" / "hooks.json"
    hooks = _load(hooks_path)
    hooks.setdefault("version", 1)
    shell_hooks = hooks.setdefault("hooks", {}).setdefault("beforeShellExecution", [])
    # The checkout is reused across runs: replace the previous run's hook rather than adding another.
    shell_hooks[:] = [h for h in shell_hooks if HOOK_MODULE not in h.get("command", "")]
    shell_hooks.append({"command": shlex.join([py, "-m", HOOK_MODULE, *common, "--format", "cursor"])})
    hooks_path.write_text(json.dumps(hooks, indent=2) + "\n")

    hide_from_git(cwd, FILES)
