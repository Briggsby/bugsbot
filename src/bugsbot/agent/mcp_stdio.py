"""The bugsbot tools as a stdio MCP server, for agents bugsbot runs as separate programs.

Usage (from a provider's MCP config): python -m bugsbot.agent.mcp_stdio --home H --run-id N --spec FILE
FILE is a ToolSpec written by the harness (see bugsbot.agent.toolspec).
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server
from mcp.server.stdio import stdio_server

from bugsbot.agent.toolspec import ToolSpec, build_tools
from bugsbot.github import GitHub, resolve_token
from bugsbot.store import Store


async def serve(home: Path, run_id: int, spec: ToolSpec) -> None:
    store = Store(home)
    settings = store.settings()
    gh = GitHub(resolve_token(settings["github_token"]))
    try:
        tools = build_tools(spec, gh, store, run_id, settings["signature"])
        server = create_sdk_mcp_server("bugsbot", tools=tools)["instance"]
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await gh.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.home, args.run_id, ToolSpec.read(args.spec)))


if __name__ == "__main__":
    main()
