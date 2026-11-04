"""`bugsbot configure`: apply settings, repos and labels from JSON, for scripted deploys.

Input (all keys optional):

    {"settings": {"model": "claude-opus-5-5", ...}, "repos": ["owner/name"], "labels": ["bugsbot"]}

Settings are overwritten; repos and labels are only ever added, so ones added in the web UI stay.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bugsbot.store import DEFAULT_SETTINGS, INTERNAL_KEYS, Store

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    pass


def apply(store: Store, data: dict[str, Any]) -> list[str]:
    """Validate everything first, then apply it. Returns a line per change (secrets not shown)."""
    unknown = set(data) - {"settings", "repos", "labels"}
    if unknown:
        raise ConfigError(f"unknown top-level keys: {', '.join(sorted(unknown))}")
    settings = data.get("settings") or {}
    repos = data.get("repos") or []
    labels = data.get("labels") or []

    for key, value in settings.items():
        if key not in DEFAULT_SETTINGS or key in INTERNAL_KEYS:
            raise ConfigError(f"unknown setting: {key}")
        expected = type(DEFAULT_SETTINGS[key])
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            settings[key] = value = float(value)
        if type(value) is not expected:
            raise ConfigError(f"setting {key} must be {expected.__name__}, got {type(value).__name__}")
    for repo in repos:
        if not isinstance(repo, str) or not REPO_RE.match(repo):
            raise ConfigError(f"repos must be 'owner/name' strings, got {repo!r}")
    for label in labels:
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"labels must be non-empty strings, got {label!r}")

    store.update_settings(settings)
    known_repos, known_labels = set(store.repos()), set(store.labels())
    for repo in repos:
        store.add_repo(repo)
    for label in labels:
        store.add_label(label.strip())
    return ([f"set {key}" for key in settings]
            + [f"added repo {r}" for r in repos if r not in known_repos]
            + [f"added label {l}" for l in labels if l.strip() not in known_labels])


def apply_json(store: Store, text: str) -> list[str]:
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("expected a JSON object")
    return apply(store, data)
