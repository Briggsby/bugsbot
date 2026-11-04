"""Agent providers. Each is a package with the same shape (see base.Provider):

- `claude`: Claude Code. Agent runs use the Claude Agent SDK; remote workers are Claude Code
  Remote Control sessions on this host, driven from the Claude app.
- `cursor`: Cursor. Agent runs use Cursor's headless CLI on this host; remote workers are
  Cursor Cloud Agents, which run in Cursor's cloud and are driven from cursor.com/agents.
"""

from __future__ import annotations

from bugsbot.providers.base import Provider
from bugsbot.providers.claude import ClaudeProvider
from bugsbot.providers.cursor import CursorProvider

PROVIDERS: dict[str, Provider] = {p.name: p for p in (ClaudeProvider(), CursorProvider())}
DEFAULT = "claude"


def get(name: str) -> Provider:
    return PROVIDERS.get(name) or PROVIDERS[DEFAULT]
