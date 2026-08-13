from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_worktree_support import GIT, SCRIPT, Harness, assert_error, git, git_out, h, payload, run

def test_publish_uses_only_owned_ref_and_verifies_remote_head(h: Harness) -> None:
    h.start()
    main_before = h.current_remote_main()
    git(h.seed, "branch", "agent/unrelated-remote", main_before)
    git(h.seed, "push", "origin", "agent/unrelated-remote")
    unrelated_before = git_out(h.origin, "rev-parse", "refs/heads/agent/unrelated-remote")
    local = h.commit_local(text="publish me")
    data = payload(h.tool("publish", "--task", "1001", "--json"))
    assert data["remote_owned_head"] == local == git_out(h.origin, "rev-parse", "refs/heads/agent/fixture")
    assert h.current_remote_main() == main_before
    assert git_out(h.origin, "rev-parse", "refs/heads/agent/unrelated-remote") == unrelated_before
    assert h.state()["published_head"] == local


def test_publish_refuses_main_or_other_branch_identity_tampering(h: Harness) -> None:
    h.start()
    state = h.state()
    state["branch"] = "main"
    h.state_path().write_text(json.dumps(state) + "\n")
    result = h.tool("publish", "--task", "1001", "--json", check=False)
    assert_error(result, "STATE_INVALID")
    state["branch"] = "agent/other-task"
    h.state_path().write_text(json.dumps(state) + "\n")
    result = h.tool("publish", "--task", "1001", "--json", check=False)
    assert_error(result, "BRANCH_MISMATCH")


def test_verify_handoff_requires_remote_equal_and_reports_four_distinct_heads(h: Harness) -> None:
    h.start()
    local = h.commit_local(text="handoff")
    result = h.tool("verify-handoff", "--task", "1001", "--json", check=False)
    assert_error(result, "HANDOFF_REMOTE_MISMATCH")
    h.tool("publish", "--task", "1001", "--json")
    current = h.advance_main()
    data = payload(h.tool("verify-handoff", "--task", "1001", "--json"))
    assert data["authoring_base_head"] == h.state()["base_sha"]
    assert data["local_implementation_head"] == local
    assert data["remote_owned_head"] == local
    assert data["current_target_head"] == current
    assert data["target_moved"] is True
    assert git_out(h.wt(), "rev-parse", "HEAD") == local


def test_dirty_handoff_refuses_claiming_durable_handoff(h: Harness) -> None:
    h.start()
    h.tool("publish", "--task", "1001", "--json")
    (h.wt() / "dirty.txt").write_text("dirty\n")
    result = h.tool("verify-handoff", "--task", "1001", "--json", check=False)
    assert_error(result, "DIRTY_HANDOFF")
