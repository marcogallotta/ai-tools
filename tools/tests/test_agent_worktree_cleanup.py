from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_worktree_support import GIT, SCRIPT, Harness, assert_error, git, git_out, h, payload, run

@pytest.mark.parametrize("disposition", ["merged", "closed", "abandoned", "superseded"])
def test_cleanup_known_dispositions_remove_clean_recoverable_worktree_and_exact_branches(h: Harness, disposition: str) -> None:
    task = {"merged": "1050", "closed": "1051", "abandoned": "1052", "superseded": "1053"}[disposition]
    branch = f"agent/cleanup-{disposition}"
    h.start(task=task, branch=branch)
    local = h.commit_local(task, disposition)
    h.tool("publish", "--task", task, "--json")
    data = payload(h.tool(
        "cleanup", "--task", task, "--branch", branch, "--expected-head", local,
        "--pr-number", "42", "--disposition", disposition, "--json"
    ))
    assert data["worktree_removed"] is True
    assert data["local_branch_removed"] is True
    assert data["remote_branch_removed"] is True
    assert not h.wt(task).exists()
    assert git(h.primary, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 1
    assert git(h.origin, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 1
    state = h.state(task)
    assert state["lifecycle"] == disposition and state["local_head"] == local
    assert state["terminal_cleanup"]["complete"] is True


def test_cleanup_refuses_dirty_only_recovery_copy_remote_ahead_and_divergence(h: Harness) -> None:
    h.start(task="1060", branch="agent/dirty-cleanup")
    h.tool("publish", "--task", "1060", "--json")
    (h.wt("1060") / "dirty.txt").write_text("dirty\n")
    result = h.tool("cleanup", "--task", "1060", "--disposition", "closed", "--json", check=False)
    assert_error(result, "DIRTY_CLEANUP")

    h.start(task="1061", branch="agent/only-copy")
    h.tool("publish", "--task", "1061", "--json")
    h.commit_local("1061", "unpublished")
    result = h.tool("cleanup", "--task", "1061", "--disposition", "closed", "--json", check=False)
    assert_error(result, "ONLY_RECOVERY_COPY")
    assert h.wt("1061").exists()

    h.start(task="1062", branch="agent/cleanup-remote-ahead")
    h.tool("publish", "--task", "1062", "--json")
    h.remote_branch_commit("agent/cleanup-remote-ahead", "cleanup remote ahead")
    result = h.tool("cleanup", "--task", "1062", "--disposition", "closed", "--json", check=False)
    assert_error(result, "EXPECTED_HEAD_MISMATCH")

    h.start(task="1063", branch="agent/cleanup-divergent")
    h.tool("publish", "--task", "1063", "--json")
    remote_base = git_out(h.origin, "rev-parse", "refs/heads/agent/cleanup-divergent")
    h.commit_local("1063", "cleanup local divergent")
    h.remote_branch_commit("agent/cleanup-divergent", "cleanup remote divergent", start=remote_base)
    result = h.tool("cleanup", "--task", "1063", "--disposition", "closed", "--json", check=False)
    assert_error(result, "ONLY_RECOVERY_COPY")


def test_cleanup_refuses_ignored_task_local_content_and_preserves_worktree(h: Harness) -> None:
    task = "1064"
    branch = "agent/cleanup-ignored"
    h.start(task=task, branch=branch)
    h.tool("publish", "--task", task, "--json")

    exclude = Path(git_out(h.wt(task), "rev-parse", "--git-path", "info/exclude"))
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("\nignored-cleanup.bin\n")
    ignored = h.wt(task) / "ignored-cleanup.bin"
    ignored.write_text("only task-local copy\n", encoding="utf-8")

    assert git_out(h.wt(task), "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert git_out(h.wt(task), "ls-files", "--others", "--ignored", "--exclude-standard") == ignored.name

    result = h.tool("cleanup", "--task", task, "--disposition", "closed", "--json", check=False)
    assert_error(result, "IGNORED_CONTENT_CLEANUP")
    assert h.wt(task).is_dir()
    assert ignored.read_text(encoding="utf-8") == "only task-local copy\n"
    assert h.state(task)["lifecycle"] == "active"
    records = git_out(h.primary, "worktree", "list", "--porcelain")
    assert str(h.wt(task)) in records and "locked" in records


@pytest.mark.parametrize("name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0"])
def test_hostile_repository_and_config_environment_fails_before_mutation(h: Harness, name: str) -> None:
    env = h.env.copy()
    env[name] = "1"
    result = run(
        ["python3", str(SCRIPT), "start", "--task", "1070", "--branch", "agent/hostile", "--base-ref", "refs/heads/main", "--base", h.base, "--json"],
        cwd=h.primary,
        env=env,
        check=False,
    )
    assert_error(result, "GIT_ENV_OVERRIDE")
    assert not h.wt("1070").exists() and not h.state_path("1070").exists()


def test_url_rewrite_config_is_rejected_but_normal_ssh_environment_is_preserved(h: Harness) -> None:
    git(h.primary, "config", "url.file:///tmp/evil.insteadOf", "git@github.com:")
    result = h.start(check=False)
    assert_error(result, "GIT_URL_REWRITE")
    git(h.primary, "config", "--unset-all", "url.file:///tmp/evil.insteadOf")
    # Normal SSH authentication/config environment remains available: fixture transport itself depends on it.
    h.start()
    assert h.wt().exists()


def test_status_is_read_only_and_exec_enters_exact_owned_path(h: Harness) -> None:
    h.start()
    before = h.state_path().read_bytes()
    data = payload(h.tool("status", "--task", "1001", "--json"))
    assert data["ok"] is True and data["worktree"] == str(h.wt())
    assert h.state_path().read_bytes() == before
    result = h.tool("exec", "--task", "1001", "--", "pwd")
    assert result.stdout.strip() == str(h.wt())


def _terminal_args(task: str, branch: str, head: str, disposition: str = "closed") -> list[str]:
    return [
        "cleanup", "--task", task, "--branch", branch, "--expected-head", head,
        "--pr-number", "42", "--disposition", disposition, "--json",
    ]


def test_cleanup_expected_head_mismatch_refuses_before_any_mutation(h: Harness) -> None:
    task = "1080"
    branch = "agent/cleanup-head-moved"
    h.start(task=task, branch=branch)
    head = h.commit_local(task, "published terminal")
    h.tool("publish", "--task", task, "--json")
    moved = h.remote_branch_commit(branch, "remote reused")
    assert moved != head

    result = h.raw_tool(*_terminal_args(task, branch, head), check=False)
    assert_error(result, "EXPECTED_HEAD_MISMATCH")
    assert h.wt(task).exists()
    assert git_out(h.primary, "rev-parse", f"refs/heads/{branch}") == head
    assert git_out(h.origin, "rev-parse", f"refs/heads/{branch}") == moved
    assert h.state(task)["lifecycle"] == "active"


def test_cleanup_restart_reconciles_crash_after_worktree_remove(h: Harness) -> None:
    task = "1081"
    branch = "agent/cleanup-restart-worktree"
    h.start(task=task, branch=branch)
    head = h.commit_local(task, "terminal")
    h.tool("publish", "--task", task, "--json")
    state = h.state(task)
    state["lifecycle"] = "closed"
    state["disposition"] = "closed"
    state["terminal_cleanup"] = {
        "schema": "dish-terminal-cleanup-v1", "task_gid": task, "pr_number": 42,
        "disposition": "closed", "branch": branch, "expected_head": head,
        "started_at": "2026-08-14T00:00:00+00:00", "worktree_removed": False,
        "local_branch_removed": False, "remote_branch_removed": False, "complete": False,
    }
    h.state_path(task).write_text(json.dumps(state) + "\n", encoding="utf-8")
    git(h.primary, "worktree", "unlock", str(h.wt(task)))
    git(h.primary, "worktree", "remove", str(h.wt(task)))

    data = payload(h.raw_tool(*_terminal_args(task, branch, head)))
    assert data["worktree_removed"] is True
    assert data["local_branch_removed"] is True
    assert data["remote_branch_removed"] is True
    assert h.state(task)["terminal_cleanup"]["complete"] is True


def test_cleanup_restart_reconciles_crash_after_local_branch_delete(h: Harness) -> None:
    task = "1082"
    branch = "agent/cleanup-restart-branch"
    h.start(task=task, branch=branch)
    head = h.commit_local(task, "terminal")
    h.tool("publish", "--task", task, "--json")
    state = h.state(task)
    state["lifecycle"] = "closed"
    state["disposition"] = "closed"
    state["terminal_cleanup"] = {
        "schema": "dish-terminal-cleanup-v1", "task_gid": task, "pr_number": 42,
        "disposition": "closed", "branch": branch, "expected_head": head,
        "started_at": "2026-08-14T00:00:00+00:00", "worktree_removed": True,
        "local_branch_removed": False, "remote_branch_removed": False, "complete": False,
    }
    h.state_path(task).write_text(json.dumps(state) + "\n", encoding="utf-8")
    git(h.primary, "worktree", "unlock", str(h.wt(task)))
    git(h.primary, "worktree", "remove", str(h.wt(task)))
    git(h.primary, "update-ref", "-d", f"refs/heads/{branch}", head)

    data = payload(h.raw_tool(*_terminal_args(task, branch, head)))
    assert data["worktree_removed"] is True
    assert data["local_branch_removed"] is True
    assert data["remote_branch_removed"] is True
    assert h.state(task)["terminal_cleanup"]["complete"] is True


def test_cleanup_is_idempotent_after_complete_terminal_cleanup(h: Harness) -> None:
    task = "1083"
    branch = "agent/cleanup-idempotent"
    h.start(task=task, branch=branch)
    head = h.commit_local(task, "terminal")
    h.tool("publish", "--task", task, "--json")
    first = payload(h.raw_tool(*_terminal_args(task, branch, head)))
    second = payload(h.raw_tool(*_terminal_args(task, branch, head)))
    assert first["remote_branch_removed"] is True
    assert second["worktree_removed"] is True
    assert second["local_branch_removed"] is True
    assert second["remote_branch_removed"] is True
    assert h.state(task)["terminal_cleanup"]["complete"] is True
