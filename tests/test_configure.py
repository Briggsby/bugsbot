import pytest

from bugsbot.configure import ConfigError, apply, apply_json
from bugsbot.store import Store


def test_apply_sets_settings_and_adds_repos_labels(tmp_path):
    store = Store(tmp_path)
    store.add_repo("me/kept")
    changes = apply(store, {"settings": {"model": "claude-sonnet-5-5", "max_budget_usd_per_run": 5,
                                         "anthropic_api_key": "sk-ant-x"},
                            "repos": ["me/new", "me/kept"], "labels": ["bugsbot"]})
    s = store.settings()
    assert s["model"] == "claude-sonnet-5-5" and s["max_budget_usd_per_run"] == 5.0
    assert s["anthropic_api_key"] == "sk-ant-x"
    assert set(store.repos()) == {"me/kept", "me/new"} and store.labels() == ["bugsbot"]
    assert "added repo me/new" in changes and "added repo me/kept" not in changes
    assert not any("sk-ant" in c for c in changes)


@pytest.mark.parametrize("data", [
    {"settings": {"session_secret": "x"}},
    {"settings": {"nope": 1}},
    {"settings": {"polling_enabled": "yes"}},
    {"repos": ["not a repo"]},
    {"labels": [""]},
    {"other": 1},
])
def test_apply_rejects_bad_input_without_changing_anything(tmp_path, data):
    store = Store(tmp_path)
    with pytest.raises(ConfigError):
        apply(store, {"repos": ["me/a"], **data})
    assert store.repos() == []


def test_apply_json_errors(tmp_path):
    with pytest.raises(ConfigError):
        apply_json(Store(tmp_path), "[1]")
    assert apply_json(Store(tmp_path), "") == []
