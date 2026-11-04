"""Claude agent runs via the Claude Agent SDK, with bugsbot's tools in-process."""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from bugsbot.agent.hook import decide
from bugsbot.agent.policy import check_command
from bugsbot.providers.base import AgentJob, AgentOutcome, Log
from bugsbot.workspace import agent_env


def make_guard(log: Log):
    """PreToolUse hook that denies forbidden shell commands."""
    async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        command = (input_data.get("tool_input") or {}).get("command", "")
        if reason := check_command(command):
            log("blocked", f"{reason}\n$ {command}")
        return decide("claude", input_data) or {}
    return guard


def build_options(job: AgentJob) -> ClaudeAgentOptions:
    settings = job.settings
    env = agent_env(job.home, settings["git_author_name"], settings["git_author_email"])
    if settings["anthropic_api_key"]:
        env["ANTHROPIC_API_KEY"] = settings["anthropic_api_key"]
    return ClaudeAgentOptions(
        cwd=str(job.cwd),
        model=settings["model"] or None,
        effort=settings["effort"] or None,
        system_prompt={"type": "preset", "preset": "claude_code", "append": job.rules},
        setting_sources=["project"],
        permission_mode="bypassPermissions",
        max_turns=int(settings["max_turns_per_run"]) or None,
        max_budget_usd=float(settings["max_budget_usd_per_run"]) or None,
        mcp_servers={"bugsbot": create_sdk_mcp_server("bugsbot", tools=job.tools)},
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[make_guard(job.log)])]},
        env=env,
        stderr=lambda line: job.log("stderr", line),
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


def outcome(result: ResultMessage | None) -> AgentOutcome:
    if result is None:
        return AgentOutcome(ok=False, error="agent produced no result")
    text = (result.result or "").strip()
    error = f"{result.subtype}: {'; '.join(result.errors or [])}" if result.is_error else ""
    return AgentOutcome(ok=not result.is_error, result=text, error=error,
                        num_turns=result.num_turns, cost_usd=result.total_cost_usd)


async def run_agent(job: AgentJob) -> AgentOutcome:
    result: ResultMessage | None = None
    async with ClaudeSDKClient(options=build_options(job)) as client:
        await client.query(job.prompt)
        async for msg in client.receive_response():
            log_message(job.log, msg)
            if isinstance(msg, ResultMessage):
                result = msg
    return outcome(result)
