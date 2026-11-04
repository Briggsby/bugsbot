"""The shell-command check as a hook command, for agent processes bugsbot doesn't run in-process.

Usage: python -m bugsbot.agent.hook --home H --run-id N --format claude|cursor
Reads the provider's hook input JSON on stdin and prints its decision format:
- claude: a PreToolUse hook; prints a deny decision, or nothing to allow.
- cursor: a beforeShellExecution hook; always prints a decision (Cursor blocks on invalid output).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bugsbot.agent.policy import check_command
from bugsbot.store import Store


def decide(fmt: str, payload: dict) -> dict | None:
    if fmt == "cursor":
        command = payload.get("command") or ""
    else:
        command = (payload.get("tool_input") or {}).get("command") or ""
    reason = check_command(command)
    if fmt == "cursor":
        if reason is None:
            return {"permission": "allow"}
        return {"permission": "deny", "user_message": f"bugsbot policy: {reason}",
                "agent_message": f"bugsbot policy: {reason}"}
    if reason is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"bugsbot policy: {reason}"}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--format", choices=["claude", "cursor"], default="claude")
    args = parser.parse_args()

    payload = json.load(sys.stdin)
    decision = decide(args.format, payload)
    command = payload.get("command") or (payload.get("tool_input") or {}).get("command") or ""
    if reason := check_command(command):
        Store(args.home).add_event(args.run_id, "blocked", f"{reason}\n$ {command}")
    if decision:
        print(json.dumps(decision))


if __name__ == "__main__":
    main()
