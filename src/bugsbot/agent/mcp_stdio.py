"""The bugsbot GitHub tools as a stdio MCP server, for Claude Code processes not driven via the SDK.

Usage (from .mcp.json): python -m bugsbot.agent.mcp_stdio --home H --run-id N --repo O/R --cwd PATH
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server
from mcp.server.stdio import stdio_server

from bugsbot.agent.tools import ToolKit
from bugsbot.github import GitHub, resolve_token
from bugsbot.store import Store
from bugsbot.workspace import Workspace


async def serve(home: Path, run_id: int, repo: str, cwd: Path) -> None:
    store = Store(home)
    settings = store.settings()
    gh = GitHub(resolve_token(settings["github_token"]))
    try:
        default_branch = (await gh.repo(repo))["default_branch"]
        workspace = Workspace.at(cwd, repo, gh.token)
        kit = ToolKit(gh=gh, repo=repo, signature=settings["signature"], workspace=workspace,
                      protected={default_branch, "main", "master"}, base_branch=default_branch,
                      log=lambda type_, text: store.add_event(run_id, type_, text))
        server = create_sdk_mcp_server(
            "bugsbot", tools=[kit.push_branch(), kit.open_pull_request(labels=None), kit.comment()])["instance"]
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await gh.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.home, args.run_id, args.repo, args.cwd))


if __name__ == "__main__":
    main()
