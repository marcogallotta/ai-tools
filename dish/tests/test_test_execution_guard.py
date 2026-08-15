from pathlib import Path

import pytest

from test_selection import execution_guard


def _fake_git(values):
    return lambda _root, *args: values[args]


def test_primary_checkout_is_refused(monkeypatch):
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--git-dir"): ".git",
        ("rev-parse", "--git-common-dir"): ".git",
        ("branch", "--show-current"): "feature",
        ("rev-parse", "HEAD"): "a" * 40,
    }))
    with pytest.raises(execution_guard.TestExecutionRefused, match="protected primary"):
        execution_guard.require_safe_test_checkout(Path("/repo"))


def test_main_branch_is_refused_even_in_linked_worktree(monkeypatch):
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/task",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("branch", "--show-current"): "main",
        ("rev-parse", "HEAD"): "a" * 40,
    }))
    with pytest.raises(execution_guard.TestExecutionRefused, match="protected primary"):
        execution_guard.require_safe_test_checkout(Path("/worktree"))


def test_exact_candidate_head_is_required(monkeypatch):
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/task",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("branch", "--show-current"): "agent/task",
        ("rev-parse", "HEAD"): "a" * 40,
    }))
    with pytest.raises(execution_guard.TestExecutionRefused, match="candidate HEAD mismatch"):
        execution_guard.require_safe_test_checkout(Path("/worktree"), expected_head="b" * 40)
    assert execution_guard.require_safe_test_checkout(
        Path("/worktree"), expected_head="a" * 40
    ) == "a" * 40
