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
    assert_error(result, "INVALID_BRANCH")
    state["branch"] = "agent/other-task"
    h.state_path().write_text(json.dumps(state) + "\n")
    result = h.tool("publish", "--task", "1001", "--json", check=False)
    assert result.returncode != 0
    assert "ERROR BRANCH_MISMATCH:" in result.stderr or "ERROR OWNERSHIP_AMBIGUOUS:" in result.stderr


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


def test_stale_global_claim_is_rejected_before_push(h: Harness) -> None:
    h.start()
    local_head = h.commit_local(text="candidate")
    claim_files = list((h.home / ".local/state/dish/worktrees/claims").glob("*/1001.json"))
    assert len(claim_files) == 1
    local_claim = json.loads(claim_files[0].read_text())
    old_global = str(local_claim["global_claim_id"])

    from implementation_claim_lib.orchestration import NullAsanaMirror
    from implementation_claim_lib.service import ClaimCoordinator
    from implementation_claim_lib.store import ClaimStore

    c = ClaimCoordinator(
        ClaimStore(h.root / "global-claims.sqlite3"),
        repository="marcogallotta/ai-tools",
        asana=NullAsanaMirror(),
    )
    current = c.status("1001")
    assert current is not None and current["claim_id"] == old_global
    replacement = c.takeover({
        "repository": "marcogallotta/ai-tools",
        "task_gid": "1001",
        "expected_claim_id": old_global,
        "owner": "replacement",
        "session_id": "replacement-session",
        "host": "other-host",
        "authoring_base_sha": str(current["authoring_base_sha"]),
        "reason": "fixture cross-host takeover",
        "liveness_evidence": "fixture explicit coordinator handoff",
    }, recovery_authorized=True)
    assert replacement["claim_id"] != old_global

    result = h.tool("publish", "--task", "1001", "--json", check=False)
    assert_error(result, "OWNERSHIP_CONFLICT")
    remote = git(h.origin, "rev-parse", "--verify", "refs/heads/agent/fixture", check=False)
    assert remote.returncode != 0
    assert local_head == git_out(h.wt(), "rev-parse", "HEAD")
