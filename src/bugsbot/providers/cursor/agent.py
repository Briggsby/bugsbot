"""Cursor agent runs via Cursor's headless CLI (`agent -p`) on this host."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

from bugsbot.providers.base import Agent, AgentOutcome
from bugsbot.providers.cursor import guardrails
from bugsbot.providers.cursor.credentials import find_cli
from bugsbot.providers.cursor.hook import CursorHook
from bugsbot.workspace import agent_env

MAX_PROMPT_ARG = 100_000  # bytes; Linux caps one argument at 128 KiB, so longer prompts go in a file
STDERR_TAIL = 20


def _tool_name(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """`{"shellToolCall": {...}}` -> ("shell", {...}); other tools use `function: {name, arguments}`."""
    if "function" in call:
        fn = call["function"]
        return fn.get("name", "tool"), fn
    key = next(iter(call), "tool")
    return key.removesuffix("ToolCall"), call.get(key) or {}


class CursorAgent(Agent):
    """Runs the job with Cursor's CLI as a child process.

    The CLI takes no tools or hooks on its command line, so bugsbot writes them into the
    checkout's Cursor config (guardrails.py): its tools as a stdio MCP server, and its
    shell-command check as a beforeShellExecution hook (CursorHook, via bugsbot.agent.hook).
    Its rules go at the top of the prompt. Progress comes back as stream-json events
    (https://cursor.com/docs/cli/reference/output-format), logged like a Claude run's. The CLI
    reports no turn count or cost.
    """

    hook = CursorHook

    async def run(self) -> AgentOutcome:
        cli = find_cli()
        if not cli:
            return AgentOutcome(ok=False, error="the Cursor CLI isn't installed on this host "
                                                "(install it with `curl https://cursor.com/install -fsS | bash`)")
        job = self.job
        run_dir = job.home / "runs" / str(job.run_id)
        guardrails.install(job.cwd, job.home, job.run_id, job.tool_spec.write(run_dir / "tools.json"))
        cmd = self.command(cli, self.prompt_arg())
        job.log("info", f"$ {' '.join(cmd[:-1])} <prompt>")

        self.result: dict[str, Any] | None = None
        self.stderr: list[str] = []
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=job.cwd, env=self.environment(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            limit=16 * 1024 * 1024)  # stream-json lines carry whole file contents
        try:
            await asyncio.gather(self._read_stdout(proc), self._read_stderr(proc))
            code = await proc.wait()
        finally:
            if proc.returncode is None:  # cancelled or timed out: don't leave the agent running
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        return self.outcome(code)

    def prompt_arg(self) -> str:
        """The prompt (rules first), or a pointer to a file holding it if it's too long for argv."""
        job = self.job
        prompt = f"{job.rules.strip()}\n\n---\n\n{job.prompt}"
        if len(prompt.encode()) <= MAX_PROMPT_ARG:
            return prompt
        prompt_file = job.home / "runs" / str(job.run_id) / "prompt.md"
        prompt_file.write_text(prompt)
        return f"Your full instructions are in {prompt_file}. Read all of that file first, then follow it."

    def command(self, cli: str, prompt_arg: str) -> list[str]:
        cmd = [cli, "-p", "--output-format", "stream-json", "--force", "--trust", "--approve-mcps",
               "--workspace", str(self.job.cwd)]
        if model := self.job.settings["cursor_model"]:
            cmd += ["--model", model]
        return [*cmd, prompt_arg]

    def environment(self) -> dict[str, str]:
        """No GitHub credentials (as for Claude runs); the Cursor API key if one is set."""
        settings = self.job.settings
        env = {**os.environ, **agent_env(self.job.home, settings["git_author_name"], settings["git_author_email"])}
        if settings["cursor_api_key"]:
            env["CURSOR_API_KEY"] = settings["cursor_api_key"]
        return env

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        async for line in proc.stdout:
            self.log_event(line.decode(errors="replace"))

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                self.job.log("stderr", text)
                self.stderr[:] = [*self.stderr[-(STDERR_TAIL - 1):], text]

    def log_event(self, raw: str) -> None:
        """Record one stream-json line as run events, keeping the final result."""
        log = self.job.log
        raw = raw.strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            log("stdout", raw[:3000])
            return
        kind = event.get("type")
        if kind == "assistant":
            text = "".join(c.get("text", "") for c in event.get("message", {}).get("content", [])
                           if c.get("type") == "text")
            if text.strip():
                log("assistant", text)
        elif kind == "tool_call":
            name, body = _tool_name(event.get("tool_call") or {})
            if event.get("subtype") == "started":
                log("tool_use", f"{name} {json.dumps(body.get('args', body))[:3000]}")
            elif event.get("subtype") == "completed":
                result = body.get("result") or {}
                failed = "success" not in result and bool(result)
                log("tool_error" if failed else "tool_result", json.dumps(result)[:3000])
        elif kind == "result":
            self.result = event
            log("result", f"{event.get('subtype', '')} duration={event.get('duration_ms', 0) / 1000:.0f}s")

    def outcome(self, code: int) -> AgentOutcome:
        result = self.result or {}
        text = (result.get("result") or "").strip()
        if self.result and not result.get("is_error") and code == 0:
            return AgentOutcome(ok=True, result=text)
        detail = "\n".join(self.stderr[-5:]) or text or "no output"
        return AgentOutcome(ok=False, result=text, error=f"Cursor CLI exited with status {code}: {detail}")
