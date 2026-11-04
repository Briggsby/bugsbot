"""Validate an Anthropic API key (and the configured model) before agents rely on it."""

from __future__ import annotations

import anthropic


async def check_api_key(api_key: str, model: str) -> str | None:
    """Returns None if the key works with `model`, else a human-readable problem.

    Uses the Models API, which costs nothing.
    """
    if not api_key.startswith("sk-ant-"):
        return "that doesn't look like an Anthropic API key (they start with sk-ant-)"
    async with anthropic.AsyncAnthropic(api_key=api_key, max_retries=1, timeout=15) as client:
        try:
            await client.models.retrieve(model)
        except anthropic.AuthenticationError:
            return "Anthropic rejected this API key (it may be mistyped, revoked, or from another account)"
        except anthropic.PermissionDeniedError:
            return f"this API key isn't permitted to use {model}"
        except anthropic.NotFoundError:
            return f"model {model!r} wasn't found; check the Model setting"
        except anthropic.APIStatusError as e:
            return f"Anthropic API error {e.status_code}: {e.message}"
        except anthropic.APIConnectionError:
            return "couldn't reach api.anthropic.com from this host"
    return None


LOGIN_HINT = (
    "Claude Code had no working credentials. Set a valid Anthropic API key under Settings, "
    "or log in to Claude Code on the bugsbot host (run `claude`, then /login)."
)


def needs_login(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in ("/login", "invalid api key", "not logged in", "must be logged in"))
