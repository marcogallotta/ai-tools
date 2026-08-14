from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_worktree_support import GIT, SCRIPT, Harness, assert_error, git, git_out, h, payload, run

def test_fresh_start_creates_locked_owned_worktree_and_compatible_agent_reference(h: Harness) -> None:
    agent_path = h.agent_file("claude-1", custom="preserve")
    base = h.current_remote_main()
    result = h.start(agent="claude-1", base=base)
    data = payload(result)
    state = h.state()
    assert data["base_sha"] == base
    assert data["worktree"] == str(h.wt())
    assert h.wt().is_dir() and not str(h.wt()).startswith(str(h.primary) + os.sep)
    assert git_out(h.wt(), "rev-parse", "HEAD") == base
    assert git_out(h.wt(), "symbolic-ref", "--short", "HEAD") == "agent/fixture"
    porcelain = git_out(h.primary, "worktree", "list", "--porcelain")
    assert str(h.wt()) in porcelain and "locked" in porcelain
    assert state["base_sha"] == base and state["lifecycle"] == "active"
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    assert agent["workspace"] == "legacy" and agent["custom"] == "preserve"
    assert agent["active_worktree"]["task_gid"] == "1001"


def test_stale_dirty_primary_fetches_exact_current_base_without_moving_local_refs(h: Harness) -> None:
    old_main = h.base
    git(h.primary, "branch", "agent/unrelated", old_main)
    (h.primary / "operator-dirty.txt").write_text("do not touch\n", encoding="utf-8")
    new_main = h.advance_main()
    assert not git(h.primary, "cat-file", "-e", f"{new_main}^{{commit}}", check=False).returncode == 0
    other_before = git_out(h.primary, "rev-parse", "refs/heads/agent/unrelated")
    h.start(base=new_main)
    assert git_out(h.primary, "rev-parse", "refs/heads/main") == old_main
    assert git_out(h.primary, "rev-parse", "refs/heads/agent/unrelated") == other_before
    assert (h.primary / "operator-dirty.txt").read_text() == "do not touch\n"
    assert git_out(h.wt(), "rev-parse", "HEAD") == new_main
    assert not (h.primary / ".git" / "FETCH_HEAD").exists() or new_main not in (h.primary / ".git" / "FETCH_HEAD").read_text(errors="ignore")


def test_local_primary_main_ahead_is_not_authoring_authority(h: Harness) -> None:
    remote_base = h.current_remote_main()
    (h.primary / "operator-only.txt").write_text("operator local main\n", encoding="utf-8")
    git(h.primary, "add", "operator-only.txt")
    git(h.primary, "commit", "-m", "operator local main ahead")
    ahead = git_out(h.primary, "rev-parse", "refs/heads/main")
    assert ahead != remote_base
    h.start(base=remote_base)
    assert git_out(h.primary, "rev-parse", "refs/heads/main") == ahead
    assert git_out(h.wt(), "rev-parse", "HEAD") == remote_base


def test_stale_supplied_base_fails_before_branch_or_worktree_creation(h: Harness) -> None:
    stale = h.base
    current = h.advance_main()
    result = h.start(base=stale, check=False)
    assert_error(result, "STALE_HANDOFF_BASE")
    assert stale != current
    assert not h.wt().exists()
    assert git(h.primary, "show-ref", "--verify", "--quiet", "refs/heads/agent/fixture", check=False).returncode != 0
    assert not h.state_path().exists()


def test_missing_remote_base_ref_and_wrong_origin_fail_closed(h: Harness) -> None:
    result = h.tool("start", "--task", "1001", "--branch", "agent/fixture", "--base-ref", "refs/heads/missing", "--base", h.base, check=False)
    assert_error(result, "REMOTE_REF_MISSING")
    git(h.primary, "remote", "set-url", "origin", "git@github.com:someone-else/ai-tools.git")
    result = h.start(check=False)
    assert_error(result, "ORIGIN_IDENTITY")


def test_existing_remote_owned_branch_without_state_is_collision(h: Harness) -> None:
    git(h.seed, "branch", "agent/remote-collision", h.current_remote_main())
    git(h.seed, "push", "origin", "agent/remote-collision")
    result = h.start(task="1004", branch="agent/remote-collision", check=False)
    assert_error(result, "REMOTE_BRANCH_COLLISION")
    assert not h.wt("1004").exists() and not h.state_path("1004").exists()


def test_exact_state_start_is_resume_but_collisions_without_state_fail(h: Harness) -> None:
    first = payload(h.start())
    second = payload(h.start())
    assert second["worktree"] == first["worktree"]
    assert len([r for r in git_out(h.primary, "worktree", "list", "--porcelain").split("\n\n") if str(h.wt()) in r]) == 1

    task = "1002"
    git(h.primary, "branch", "agent/collision", h.base)
    result = h.start(task=task, branch="agent/collision", check=False)
    assert_error(result, "BRANCH_COLLISION")

    task = "1003"
    path = h.wt(task)
    path.mkdir(parents=True)
    (path / "foreign").write_text("x", encoding="utf-8")
    result = h.start(task=task, branch="agent/path-collision", check=False)
    assert_error(result, "WORKTREE_COLLISION")


def test_branch_checked_out_elsewhere_and_interrupted_registered_creation_fail_closed(h: Harness) -> None:
    other = h.root / "other-worktree"
    git(h.primary, "worktree", "add", "-b", "agent/in-use", str(other), h.base)
    result = h.start(task="1010", branch="agent/in-use", check=False)
    assert_error(result, "BRANCH_CHECKED_OUT")

    task = "1011"
    expected = h.wt(task)
    expected.parent.mkdir(parents=True, exist_ok=True)
    git(h.primary, "worktree", "add", "--lock", "-b", "agent/interrupted", str(expected), h.base)
    result = h.start(task=task, branch="agent/interrupted", check=False)
    assert_error(result, "WORKTREE_COLLISION")
    assert not h.state_path(task).exists()


def test_empty_precreation_path_is_recoverable_before_registration(h: Harness) -> None:
    task = "1012"
    h.wt(task).mkdir(parents=True)
    h.start(task=task, branch="agent/precreated")
    assert git_out(h.wt(task), "symbolic-ref", "--short", "HEAD") == "agent/precreated"


def test_concurrent_first_start_serializes_to_one_task_worktree(h: Harness) -> None:
    task = "1020"
    branch = "agent/race"
    base = h.base
    processes = []
    for agent in ("race-a", "race-b"):
        h.agent_file(agent)
        start_argv = [
            "python3", str(SCRIPT), "start",
            "--task", task, "--branch", branch,
            "--base-ref", "refs/heads/main", "--base", base,
            "--agent-id", agent, "--json",
        ]
        child = [
            "python3", "-c",
            "import subprocess,time,sys; subprocess.check_call(sys.argv[1:]); time.sleep(.5)",
            *start_argv,
        ]
        processes.append(
            subprocess.Popen(
                [
                    "python3", str(SCRIPT), "claim",
                    "--task", task, "--branch", branch, "--agent-id", agent,
                    "--", *child,
                ],
                cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        )

    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        results.append((process.returncode, stdout, stderr))

    assert sum(code == 0 for code, _, _ in results) == 1, results
    loser = next(item for item in results if item[0] != 0)
    assert any(
        marker in loser[2]
        for marker in ("ERROR OWNERSHIP_CONFLICT:", "ERROR OWNERSHIP_CLAIMED:", "ERROR OWNER_HANDOFF_REQUIRED:")
    )
    records = git_out(h.primary, "worktree", "list", "--porcelain").split("\n\n")
    assert sum(str(h.wt(task)) in record for record in records) == 1
