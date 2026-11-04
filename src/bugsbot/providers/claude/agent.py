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

from bugsbot.providers.base import Agent, AgentOutcome
from bugsbot.providers.claude.hook import ClaudeHook
from bugsbot.workspace import agent_env


class ClaudeAgent(Agent):
    """Runs the job in a Claude Agent SDK session.

    Everything bugsbot adds is passed to the SDK directly: its rules as a system prompt
    addition, its tools as an in-process MCP server, and its shell-command check as a
    PreToolUse hook callback (`guard`), so nothing is written into the checkout.
    """

    hook = ClaudeHook

    async def run(self) -> AgentOutcome:
        result: ResultMessage | None = None
        async with ClaudeSDKClient(options=self.options()) as client:
            await client.query(self.job.prompt)
            async for msg in client.receive_response():
                self.log_message(msg)
                if isinstance(msg, ResultMessage):
                    result = msg
        return self.outcome(result)

    def options(self) -> ClaudeAgentOptions:
        job, settings = self.job, self.job.settings
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
            hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[self.guard])]},
            env=env,
            stderr=lambda line: job.log("stderr", line),
        )

    async def guard(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        """The SDK's PreToolUse callback: the same check and answer as the command-line hook."""
        command, reason, response = self.hook.evaluate(input_data)
        if reason:
            self.job.log("blocked", f"{reason}\n$ {command}")
        return response or {}

    def log_message(self, msg: Any) -> None:
        """Record one SDK message as run events."""
        log = self.job.log
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

    @staticmethod
    def outcome(result: ResultMessage | None) -> AgentOutcome:
        if result is None:
            return AgentOutcome(ok=False, error="agent produced no result")
        error = f"{result.subtype}: {'; '.join(result.errors or [])}" if result.is_error else ""
        return AgentOutcome(ok=not result.is_error, result=(result.result or "").strip(), error=error,
                            num_turns=result.num_turns, cost_usd=result.total_cost_usd)
