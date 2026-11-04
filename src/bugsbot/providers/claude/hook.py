"""bugsbot's shell-command check as a Claude Code PreToolUse hook.

Claude Code sends {"tool_name": "Bash", "tool_input": {"command": ...}} and takes no output as
"allow". To deny, the hook returns a permissionDecision of "deny" with a reason the agent sees.
https://code.claude.com/docs/en/hooks
"""

from __future__ import annotations

from typing import Any

from bugsbot.providers.base import ShellHook


class ClaudeHook(ShellHook):
    @staticmethod
    def command(payload: dict[str, Any]) -> str:
        return (payload.get("tool_input") or {}).get("command") or ""

    @staticmethod
    def response(reason: str | None) -> dict[str, Any] | None:
        if reason is None:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"bugsbot policy: {reason}"}}
