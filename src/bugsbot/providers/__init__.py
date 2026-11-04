"""Agent providers. Each is a package with the same shape (see base.py):

- `claude`: Claude Code. Agent runs use the Claude Agent SDK; remote workers are Claude Code
  Remote Control sessions on this host, driven from the Claude app.
- `cursor`: Cursor. Agent runs use Cursor's headless CLI on this host; remote workers are
  Cursor Cloud Agents, which run in Cursor's cloud and are driven from cursor.com/agents.

Providers load on first use, so that importing one light module (such as a provider's
hook.py, which runs once per agent shell command) doesn't import every provider's SDK.
"""

from __future__ import annotations

import importlib
from functools import cache

from bugsbot.providers.base import Provider

CLASSES = {"claude": "bugsbot.providers.claude.provider:ClaudeProvider",
           "cursor": "bugsbot.providers.cursor.provider:CursorProvider"}
DEFAULT = "claude"


@cache
def all() -> dict[str, Provider]:  # noqa: A001 - read as providers.all()
    out: dict[str, Provider] = {}
    for name, path in CLASSES.items():
        module, cls = path.split(":")
        out[name] = getattr(importlib.import_module(module), cls)()
    return out


def get(name: str) -> Provider:
    """The provider called `name`, or the default if there's none by that name."""
    providers = all()
    return providers.get(name) or providers[DEFAULT]
