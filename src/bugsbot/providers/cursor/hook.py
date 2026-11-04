"""bugsbot's shell-command check as a Cursor beforeShellExecution hook.

Cursor sends {"command": ..., "cwd": ...} and needs an explicit {"permission": "allow" | "deny"}
answer every time: it blocks the command if a permission hook's output is missing or invalid.
https://cursor.com/docs/agent/hooks
"""

from __future__ import annotations

from typing import Any

from bugsbot.providers.base import ShellHook


class CursorHook(ShellHook):
    @staticmethod
    def command(payload: dict[str, Any]) -> str:
        return payload.get("command") or ""

    @staticmethod
    def response(reason: str | None) -> dict[str, Any]:
        if reason is None:
            return {"permission": "allow"}
        message = f"bugsbot policy: {reason}"
        return {"permission": "deny", "user_message": message, "agent_message": message}
