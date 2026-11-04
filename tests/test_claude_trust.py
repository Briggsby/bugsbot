import json
import os

import pytest

from bugsbot.providers.claude import trust as claude_trust
from bugsbot.providers.claude.trust import (TrustError, mark_remote_control_enabled, trust_folder, untrust_all_inside,
                                  untrust_folder)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    (tmp_path / "cc").mkdir()
    path = tmp_path / "cc/.claude.json"
    path.write_text(json.dumps({"numStartups": 7, "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}}}))
    os.chmod(path, 0o600)
    return path


def test_trust_and_untrust_preserve_everything_else(tmp_path, cfg):
    root = tmp_path / "workers"
    folder = root / "scratch/worker-8"
    folder.mkdir(parents=True)
    trust_folder(folder, root)
    data = json.loads(cfg.read_text())
    assert data["numStartups"] == 7 and data["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": True}
    assert data["projects"][str(folder.resolve())] == {"hasTrustDialogAccepted": True}
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"

    untrust_folder(folder, root)
    data = json.loads(cfg.read_text())
    assert str(folder.resolve()) not in data["projects"] and "/elsewhere" in data["projects"]
    untrust_folder(folder, root)  # no-op when already gone
    assert not list(cfg.parent.glob(".claude.json.bugsbot-*"))  # no temp files left behind


def test_only_folders_inside_workers_root(tmp_path, cfg):
    root = tmp_path / "workers"
    root.mkdir()
    for outside in (root, tmp_path, tmp_path / "workers-evil", root / ".." / "x"):
        with pytest.raises(TrustError, match="only folders inside"):
            trust_folder(outside, root)
    assert "elsewhere" in cfg.read_text() and str(root) not in cfg.read_text()


def test_creates_config_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    trust_folder(tmp_path / "workers/w1", tmp_path / "workers")
    assert json.loads((tmp_path / ".claude.json").read_text())["projects"]


def test_never_overwrites_unparseable_config(tmp_path, cfg):
    cfg.write_text('{"projects": {')  # e.g. caught mid-write
    with pytest.raises(TrustError, match="isn't valid JSON"):
        trust_folder(tmp_path / "workers/w1", tmp_path / "workers")
    assert cfg.read_text() == '{"projects": {'


def test_retries_when_a_concurrent_writer_clobbers(tmp_path, cfg, monkeypatch):
    real_write, calls = claude_trust._write_atomic, []

    def racing_write(path, data):
        real_write(path, data)
        calls.append(1)
        if len(calls) == 1:  # another Claude Code process rewrites the file with its stale copy
            path.write_text(json.dumps({"numStartups": 8, "projects": {}}))
    monkeypatch.setattr(claude_trust, "_write_atomic", racing_write)
    folder = tmp_path / "workers/w1"
    trust_folder(folder, tmp_path / "workers")
    data = json.loads(cfg.read_text())
    assert len(calls) == 2 and data["projects"][str(folder.resolve())]["hasTrustDialogAccepted"]
    assert data["numStartups"] == 8  # the other writer's change is kept


def test_untrust_all_inside_clears_only_leftovers(tmp_path, cfg):
    root = tmp_path / "workers"
    for name in ("a/worker-1", "scratch/worker-2"):
        trust_folder(root / name, root)
    assert untrust_all_inside(root) == 2
    assert json.loads(cfg.read_text())["projects"] == {"/elsewhere": {"hasTrustDialogAccepted": True}}


def test_mark_remote_control_enabled(cfg):
    assert mark_remote_control_enabled() is True
    data = json.loads(cfg.read_text())
    assert data["remoteDialogSeen"] is True and data["numStartups"] == 7
    assert mark_remote_control_enabled() is False  # already set: no write
