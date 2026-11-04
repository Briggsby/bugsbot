"""Claude Agent SDK plumbing shared by issue/PR runs and ad-hoc chats."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from bugsbot.agent.policy import make_guard
from bugsbot.workspace import agent_env

Log = Callable[[str, str], None]


def build_options(settings: dict[str, Any], home: Path, cwd: Path, system_append: str,
                  tools: list[Any], log: Log, *, max_turns: int | None) -> ClaudeAgentOptions:
    env = agent_env(home, settings["git_author_name"], settings["git_author_email"])
    if settings["anthropic_api_key"]:
        env["ANTHROPIC_API_KEY"] = settings["anthropic_api_key"]
    return ClaudeAgentOptions(
        cwd=str(cwd),
        model=settings["model"] or None,
        effort=settings["effort"] or None,
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_append},
        setting_sources=["project"],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        max_budget_usd=float(settings["max_budget_usd_per_run"]) or None,
        mcp_servers={"bugsbot": create_sdk_mcp_server("bugsbot", tools=tools)},
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[make_guard(log)])]},
        env=env,
        stderr=lambda line: log("stderr", line),
    )


def log_message(log: Log, msg: Any) -> None:
    """Record one SDK message as run events."""
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text.strip():
                log("assistant", block.text)
            elif isinstance(block, ToolUseBlock):
                log("tool_use", f"{block.name} {json.dumps(block.input)[:3000]}")
    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                content = block.content
                text = content if isinstance(content, str) else json.dumps(content)
                log("tool_error" if block.is_error else "tool_result", (text or "")[:3000])
    elif isinstance(msg, ResultMessage):
        log("result", f"{msg.subtype} turns={msg.num_turns} cost=${msg.total_cost_usd or 0:.4f} "
                      f"duration={msg.duration_ms / 1000:.0f}s")
