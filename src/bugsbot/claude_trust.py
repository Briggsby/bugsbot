"""Record Claude Code folder trust for remote worker checkouts.

Claude Code asks "do you trust this folder?" the first time it starts in a folder, and
trust is per folder (it isn't inherited from parents). Each remote worker gets a fresh
checkout, so bugsbot records trust for exactly that folder in Claude Code's config, the
same `hasTrustDialogAccepted` flag accepting the prompt sets, and removes it when the
worker ends.

Safety:
- Only folders inside the bugsbot workers folder are ever touched.
- The config is re-read immediately before each write, written atomically (temp file +
  rename, keeping its permissions), then re-read to confirm the change stuck. Claude Code
  processes also write this file, so a write that loses a race is retried.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

ATTEMPTS = 3


class TrustError(RuntimeError):
    pass


def claude_config_file() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        # Never overwrite a config we can't parse (it may be mid-write by Claude Code).
        raise TrustError(f"{path} isn't valid JSON right now: {e}") from e


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.bugsbot-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_scope(folder: Path, root: Path) -> str:
    folder, root = folder.resolve(), root.resolve()
    if folder == root or root not in folder.parents:
        raise TrustError(f"refusing to change trust for {folder}: only folders inside {root} are allowed")
    return str(folder)


def _update(folder: Path, root: Path, trusted: bool) -> None:
    key = _check_scope(folder, root)
    path = claude_config_file()
    for attempt in range(ATTEMPTS):
        config = _read(path)
        projects = config.setdefault("projects", {})
        if trusted:
            projects[key] = {**projects.get(key, {}), "hasTrustDialogAccepted": True}
        elif key in projects:
            del projects[key]
        else:
            return
        _write_atomic(path, config)
        time.sleep(0.05 * (attempt + 1))  # let any concurrent writer land before verifying
        now = _read(path).get("projects", {}).get(key, {})
        if bool(now.get("hasTrustDialogAccepted")) == trusted and (trusted or not now):
            return
    raise TrustError(f"couldn't record Claude Code trust for {key}: another process kept rewriting {path}")


def untrust_all_inside(root: Path) -> int:
    """Remove trust entries for every folder inside `root` (leftovers from a crash). Returns how many."""
    root_str = str(root.resolve())
    path = claude_config_file()
    config = _read(path)
    stale = [k for k in config.get("projects", {}) if k.startswith(root_str + os.sep)]
    for key in stale:
        _update(Path(key), root, trusted=False)
    return len(stale)


def trust_folder(folder: Path, root: Path) -> None:
    _update(folder, root, trusted=True)


def untrust_folder(folder: Path, root: Path) -> None:
    _update(folder, root, trusted=False)
