"""PreToolUse command hook for Claude Code processes bugsbot doesn't drive via the SDK.

Usage (from .claude/settings.local.json): python -m bugsbot.agent.hook --home H --run-id N
Reads the hook input JSON on stdin and prints a deny decision for forbidden commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from bugsbot.agent.policy import make_guard
from bugsbot.store import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()

    store = Store(args.home)
    guard = make_guard(lambda type_, text: store.add_event(args.run_id, type_, text))
    decision = asyncio.run(guard(json.load(sys.stdin), None, None))
    if decision:
        print(json.dumps(decision))


if __name__ == "__main__":
    main()
