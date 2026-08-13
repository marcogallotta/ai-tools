from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_worktree_support import GIT, SCRIPT, Harness, assert_error, git, git_out, h, payload, run

def test_resume_can_attach_agent_reference_after_state_write_and_takeover_is_explicit(h: Harness) -> None:
    h.start()
    a = h.agent_file("agent-a")
    h.tool("resume", "--task", "1001", "--agent-id", "agent-a", "--json")
    assert h.state()["owner"]["agent_id"] == "agent-a"
    b = h.agent_file("agent-b")
    result = h.tool("resume", "--task", "1001", "--agent-id", "agent-b", "--json", check=False)
    assert_error(result, "OWNER_MISMATCH")
    h.tool("resume", "--task", "1001", "--agent-id", "agent-b", "--takeover", "--json")
    assert h.state()["owner"]["agent_id"] == "agent-b"
    assert json.loads(a.read_text())["active_worktree"] is None
    assert json.loads(b.read_text())["active_worktree"]["task_gid"] == "1001"


def test_dirty_resume_preserves_files_index_and_head(h: Harness) -> None:
    h.start()
    wt = h.wt()
    (wt / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(wt, "add", "staged.txt")
    (wt / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
    before_head = git_out(wt, "rev-parse", "HEAD")
    before_cached = git_out(wt, "diff", "--cached", "--binary")
    before_status = git_out(wt, "status", "--porcelain=v1", "--untracked-files=all")
    data = payload(h.tool("resume", "--task", "1001", "--json"))
    assert data["dirty"] is True
    assert git_out(wt, "rev-parse", "HEAD") == before_head
    assert git_out(wt, "diff", "--cached", "--binary") == before_cached
    assert git_out(wt, "status", "--porcelain=v1", "--untracked-files=all") == before_status


def test_resume_rejects_wrong_branch_detached_head_and_common_dir(h: Harness) -> None:
    h.start(task="1030", branch="agent/wrong-branch")
    wt = h.wt("1030")
    git(wt, "switch", "-c", "agent/unexpected")
    result = h.tool("resume", "--task", "1030", "--json", check=False)
    assert_error(result, "BRANCH_MISMATCH")

    h.start(task="1031", branch="agent/detached")
    git(h.wt("1031"), "checkout", "--detach")
    result = h.tool("resume", "--task", "1031", "--json", check=False)
    assert_error(result, "DETACHED_HEAD")

    h.start(task="1032", branch="agent/common")
    state = h.state("1032")
    state["git_common_dir"] = str(h.root / "wrong-common")
    h.state_path("1032").write_text(json.dumps(state) + "\n")
    result = h.tool("resume", "--task", "1032", "--json", check=False)
    assert_error(result, "COMMON_DIR_MISMATCH")


def test_resume_rejects_wrong_worktree_path_and_git_dir(h: Harness) -> None:
    h.start(task="1033", branch="agent/worktree-a")
    h.start(task="1034", branch="agent/worktree-b")
    state = h.state("1033")
    state["worktree_path"] = str(h.wt("1034"))
    h.state_path("1033").write_text(json.dumps(state) + "\n")
    result = h.tool("resume", "--task", "1033", "--json", check=False)
    assert result.returncode != 0
    assert "ERROR GIT_DIR_MISMATCH:" in result.stderr or "ERROR BRANCH_MISMATCH:" in result.stderr

    state = h.state("1034")
    state["git_dir"] = str(h.root / "wrong-git-dir")
    h.state_path("1034").write_text(json.dumps(state) + "\n")
    result = h.tool("resume", "--task", "1034", "--json", check=False)
    assert_error(result, "GIT_DIR_MISMATCH")


def test_remote_relations_local_ahead_remote_ahead_and_divergent_fail_closed(h: Harness) -> None:
    # Local ahead is normal unpublished work.
    h.start(task="1040", branch="agent/local-ahead")
    h.tool("publish", "--task", "1040", "--json")
    local_head = h.commit_local("1040", "local ahead")
    data = payload(h.tool("resume", "--task", "1040", "--json"))
    assert data["remote_relation"] == "local-ahead" and data["local_head"] == local_head

    # Remote ahead stops automatic resume and leaves local HEAD fixed.
    h.start(task="1041", branch="agent/remote-ahead")
    h.tool("publish", "--task", "1041", "--json")
    before = git_out(h.wt("1041"), "rev-parse", "HEAD")
    h.remote_branch_commit("agent/remote-ahead", "remote ahead")
    result = h.tool("resume", "--task", "1041", "--json", check=False)
    assert_error(result, "REMOTE_AHEAD")
    assert git_out(h.wt("1041"), "rev-parse", "HEAD") == before

    # Divergence also stops, with no merge/rebase/reset.
    h.start(task="1042", branch="agent/divergent")
    h.tool("publish", "--task", "1042", "--json")
    remote_base = git_out(h.origin, "rev-parse", "refs/heads/agent/divergent")
    local_div = h.commit_local("1042", "local divergent")
    h.remote_branch_commit("agent/divergent", "remote divergent", start=remote_base)
    result = h.tool("resume", "--task", "1042", "--json", check=False)
    assert_error(result, "REMOTE_DIVERGED")
    assert git_out(h.wt("1042"), "rev-parse", "HEAD") == local_div


def test_main_movement_after_authoring_is_informational_and_does_not_move_task_or_local_main(h: Harness) -> None:
    stored_base = h.base
    h.start(base=stored_base)
    task_head = h.commit_local(text="implementation")
    primary_main = git_out(h.primary, "rev-parse", "refs/heads/main")
    current = h.advance_main()
    data = payload(h.tool("resume", "--task", "1001", "--json"))
    assert data["target_moved"] is True and data["current_target_head"] == current
    assert h.state()["base_sha"] == stored_base
    assert git_out(h.wt(), "rev-parse", "HEAD") == task_head
    assert git_out(h.primary, "rev-parse", "refs/heads/main") == primary_main


def test_concurrent_unrelated_fetch_and_main_movement_do_not_change_stored_authoring_base(h: Harness) -> None:
    stored_base = h.base
    h.start(base=stored_base)
    task_head = h.commit_local(text="implementation during unrelated fetch")
    primary_main = git_out(h.primary, "rev-parse", "refs/heads/main")
    current = h.advance_main("concurrent main advance")

    fetch = subprocess.Popen(
        [GIT, "-C", str(h.primary), "fetch", "origin", "refs/heads/main:refs/remotes/origin/main"],
        env=h.env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    data = payload(h.tool("resume", "--task", "1001", "--json"))
    _, fetch_stderr = fetch.communicate(timeout=20)
    assert fetch.returncode == 0, fetch_stderr

    assert data["target_moved"] is True and data["current_target_head"] == current
    assert h.state()["base_sha"] == stored_base
    assert git_out(h.wt(), "rev-parse", "HEAD") == task_head
    assert git_out(h.primary, "rev-parse", "refs/heads/main") == primary_main
    assert git_out(h.primary, "rev-parse", "refs/remotes/origin/main") == current
