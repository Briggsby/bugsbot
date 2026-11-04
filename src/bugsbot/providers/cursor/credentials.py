"""Cursor API key checks, the Cursor CLI's location, and login hints."""

from __future__ import annotations

import shutil

import httpx

from bugsbot.providers.cursor.api import CursorAPI, CursorAPIError

LOGIN_HINT = (
    "Cursor had no working credentials. Add a Cursor API key under Settings (Cursor dashboard → "
    "Integrations → User API Keys), or run `agent login` as the OS user running bugsbot."
)


def find_cli() -> str | None:
    """Cursor's CLI installs as `agent`, with `cursor-agent` as an alias; prefer the unambiguous name."""
    return shutil.which("cursor-agent") or shutil.which("agent")


async def check_api_key(api_key: str, api: CursorAPI | None = None) -> str | None:
    """None if Cursor accepts the key, else a human-readable problem."""
    api = api or CursorAPI(api_key)
    try:
        await api.list_agents(limit=1)
    except CursorAPIError as e:
        if e.status in (401, 403):
            return "Cursor rejected this API key (it may be mistyped or revoked)"
        return str(e)
    except httpx.HTTPError:
        return "couldn't reach api.cursor.com from this host"
    finally:
        await api.close()
    return None


def needs_login(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in ("not authenticated", "agent login", "invalid api key", "unauthorized",
                                      "authentication required"))
