"""Cursor agent runs via Cursor's headless CLI (`agent -p`) on this host.

bugsbot's tools reach the CLI as a stdio MCP server and its command check as a hook (see
guardrails.py); its rules go at the top of the prompt. Progress comes back as stream-json
events (https://cursor.com/docs/cli/reference/output-format), which are logged like a
Claude run's. The CLI reports no turn count or cost.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

from bugsbot.providers.base import AgentJob, AgentOutcome, Log
from bugsbot.providers.cursor import guardrails
from bugsbot.providers.cursor.credentials import find_cli
from bugsbot.workspace import agent_env

MAX_PROMPT_ARG = 100_000  # bytes; Linux caps one argument at 128 KiB, so longer prompts go in a file
STDERR_TAIL = 20


def command(cli: str, job: AgentJob, prompt_arg: str) -> list[str]:
    cmd = [cli, "-p", "--output-format", "stream-json", "--force", "--trust", "--approve-mcps",
           "--workspace", str(job.cwd)]
    if model := job.settings["cursor_model"]:
        cmd += ["--model", model]
    return [*cmd, prompt_arg]


def environment(job: AgentJob) -> dict[str, str]:
    settings = job.settings
    env = {**os.environ, **agent_env(job.home, settings["git_author_name"], settings["git_author_email"])}
    if settings["cursor_api_key"]:
        env["CURSOR_API_KEY"] = settings["cursor_api_key"]
    return env


def _tool_name(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """`{"shellToolCall": {...}}` -> ("shell", {...}); other tools use `function: {name, arguments}`."""
    if "function" in call:
        fn = call["function"]
        return fn.get("name", "tool"), fn
    key = next(iter(call), "tool")
    return key.removesuffix("ToolCall"), call.get(key) or {}


class StreamLog:
    """Turns stream-json events into run events, and keeps the final result."""

    def __init__(self, log: Log):
        self.log = log
        self.result: dict[str, Any] | None = None

    def line(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            self.log("stdout", raw[:3000])
            return
        kind = event.get("type")
        if kind == "assistant":
            text = "".join(c.get("text", "") for c in event.get("message", {}).get("content", [])
                           if c.get("type") == "text")
            if text.strip():
                self.log("assistant", text)
        elif kind == "tool_call":
            name, body = _tool_name(event.get("tool_call") or {})
            if event.get("subtype") == "started":
                self.log("tool_use", f"{name} {json.dumps(body.get('args', body))[:3000]}")
            elif event.get("subtype") == "completed":
                result = body.get("result") or {}
                failed = "success" not in result and bool(result)
                self.log("tool_error" if failed else "tool_result", json.dumps(result)[:3000])
        elif kind == "result":
            self.result = event
            self.log("result", f"{event.get('subtype', '')} duration={event.get('duration_ms', 0) / 1000:.0f}s")


async def run_agent(job: AgentJob) -> AgentOutcome:
    cli = find_cli()
    if not cli:
        return AgentOutcome(ok=False, error="the Cursor CLI isn't installed on this host "
                                            "(install it with `curl https://cursor.com/install -fsS | bash`)")
    run_dir = job.home / "runs" / str(job.run_id)
    guardrails.install(job.cwd, job.home, job.run_id, job.tool_spec.write(run_dir / "tools.json"))

    prompt = f"{job.rules.strip()}\n\n---\n\n{job.prompt}"
    prompt_arg = prompt
    if len(prompt.encode()) > MAX_PROMPT_ARG:
        prompt_file = run_dir / "prompt.md"
        prompt_file.write_text(prompt)
        prompt_arg = f"Your full instructions are in {prompt_file}. Read all of that file first, then follow it."

    cmd = command(cli, job, prompt_arg)
    job.log("info", f"$ {' '.join(cmd[:-1])} <prompt>")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=job.cwd, env=environment(job), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        limit=16 * 1024 * 1024)  # stream-json lines carry whole file contents
    stream = StreamLog(job.log)
    stderr: list[str] = []

    async def read_stdout() -> None:
        assert proc.stdout
        async for line in proc.stdout:
            stream.line(line.decode(errors="replace"))

    async def read_stderr() -> None:
        assert proc.stderr
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                job.log("stderr", text)
                stderr[:] = [*stderr[-(STDERR_TAIL - 1):], text]

    try:
        await asyncio.gather(read_stdout(), read_stderr())
        code = await proc.wait()
    finally:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    result = stream.result
    if result and not result.get("is_error") and code == 0:
        return AgentOutcome(ok=True, result=(result.get("result") or "").strip())
    detail = "\n".join(stderr[-5:]) or (result or {}).get("result") or "no output"
    return AgentOutcome(ok=False, result=((result or {}).get("result") or "").strip(),
                        error=f"Cursor CLI exited with status {code}: {detail}")
