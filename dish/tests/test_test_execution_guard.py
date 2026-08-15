from pathlib import Path

import pytest

from test_selection import execution_guard


def _fake_git(values):
    return lambda _root, *args: values[args]


def test_primary_checkout_is_refused(monkeypatch):
    monkeypatch.setattr(execution_guard, "_protected_primary_root", lambda: Path("/repo"))
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--show-toplevel"): "/repo",
        ("branch", "--show-current"): "feature",
        ("rev-parse", "HEAD"): "a" * 40,
    }))
    with pytest.raises(execution_guard.TestExecutionRefused, match="protected primary"):
        execution_guard.require_safe_test_checkout(Path("/repo"))


def test_main_branch_is_refused_even_in_linked_worktree(monkeypatch):
    monkeypatch.setattr(execution_guard, "_protected_primary_root", lambda: Path("/repo"))
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--show-toplevel"): "/worktree",
        ("branch", "--show-current"): "main",
        ("rev-parse", "HEAD"): "a" * 40,
    }))
    with pytest.raises(execution_guard.TestExecutionRefused, match="protected primary"):
        execution_guard.require_safe_test_checkout(Path("/worktree"))


def test_exact_candidate_head_is_required(monkeypatch):
    monkeypatch.setattr(execution_guard, "_protected_primary_root", lambda: Path("/repo"))
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--show-toplevel"): "/worktree",
        ("branch", "--show-current"): "agent/task",
        ("rev-parse", "HEAD"): "a" * 40,
    }))
    with pytest.raises(execution_guard.TestExecutionRefused, match="candidate HEAD mismatch"):
        execution_guard.require_safe_test_checkout(Path("/worktree"), expected_head="b" * 40)
    assert execution_guard.require_safe_test_checkout(
        Path("/worktree"), expected_head="a" * 40
    ) == "a" * 40


def test_ephemeral_detached_ci_checkout_accepts_exact_candidate(monkeypatch):
    monkeypatch.setenv("DISH_PROTECTED_PRIMARY_ROOT", "/runner/work/ai-tools")
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--show-toplevel"): "/runner/work/ai-tools",
        ("branch", "--show-current"): "",
        ("rev-parse", "HEAD"): "a" * 40,
    }))

    assert execution_guard.require_safe_test_checkout(
        Path("/runner/work/ai-tools/dish"), expected_head="a" * 40
    ) == "a" * 40


def test_environment_cannot_redefine_protected_primary(monkeypatch):
    protected_primary = execution_guard._protected_primary_root()
    monkeypatch.setenv("DISH_PROTECTED_PRIMARY_ROOT", "/somewhere/else")
    monkeypatch.setattr(execution_guard, "_git", _fake_git({
        ("rev-parse", "--show-toplevel"): str(protected_primary),
        ("branch", "--show-current"): "",
        ("rev-parse", "HEAD"): "a" * 40,
    }))

    with pytest.raises(execution_guard.TestExecutionRefused, match="protected primary"):
        execution_guard.require_safe_test_checkout(
            protected_primary, expected_head="a" * 40
        )
