"""Entry point for shell-command hooks: `python -m bugsbot.agent.hook --home H --run-id N --provider P`.

Agents bugsbot runs as separate programs (Cursor's CLI, Claude Code Remote Control) can't be
given bugsbot's command check in-process. Instead bugsbot registers this command as a hook in the
checkout's provider config (see each provider's guardrails.py). The provider runs it before every
shell command the agent wants to run, with the command as JSON on stdin, and obeys the answer
printed on stdout.

This module only routes: the provider's ShellHook subclass (bugsbot.providers.<P>.hook) reads its
input format, applies bugsbot.agent.policy.check_command, and writes its answer format. Blocked
commands are also logged to the run, so they show in the web UI.

It starts once per shell command, so it imports only what it needs: not the providers' SDKs.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from bugsbot.providers.base import ShellHook
from bugsbot.store import Store

HOOKS = {"claude": "bugsbot.providers.claude.hook:ClaudeHook",
         "cursor": "bugsbot.providers.cursor.hook:CursorHook"}


def load(provider: str) -> type[ShellHook]:
    module, name = HOOKS[provider].split(":")
    return getattr(importlib.import_module(module), name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    # --format is the old name, still used by hooks installed in existing checkouts.
    parser.add_argument("--provider", "--format", choices=sorted(HOOKS), default="claude")
    args = parser.parse_args()

    command, reason, response = load(args.provider).evaluate(json.load(sys.stdin))
    if reason:
        Store(args.home).add_event(args.run_id, "blocked", f"{reason}\n$ {command}")
    if response is not None:
        print(json.dumps(response))


if __name__ == "__main__":
    main()
