from __future__ import annotations

import json
import os
import subprocess
import threading
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



def test_takeover_recovery_token_stops_at_claim_wrapper_boundary(h: Harness) -> None:
    from implementation_claim_lib.client import ClaimServiceClient
    from implementation_claim_lib.errors import ClaimError
    from implementation_claim_lib.http_api import ClaimHTTPServer
    from implementation_claim_lib.orchestration import NullAsanaMirror
    from implementation_claim_lib.service import ClaimCoordinator
    from implementation_claim_lib.store import ClaimStore

    task = "1007"
    branch = "agent/recovery-boundary"
    base = h.current_remote_main()
    for agent in ("a", "b"):
        h.agent_file(agent)

    ordinary_token = "ordinary-service-token"
    recovery_token = "orchestration-recovery-secret"
    coordinator = ClaimCoordinator(
        ClaimStore(h.root / "http-global-claims.sqlite3"),
        repository="marcogallotta/ai-tools",
        asana=NullAsanaMirror(),
    )
    server = ClaimHTTPServer(("127.0.0.1", 0), coordinator, ordinary_token, recovery_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service_url = f"http://127.0.0.1:{server.server_address[1]}"

    parent_env = h.env.copy()
    parent_env.pop("DISH_IMPLEMENTATION_CLAIM_TEST_DB", None)
    parent_env.pop("DISH_IMPLEMENTATION_CLAIM_TESTING", None)
    parent_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    parent_env.update(
        DISH_IMPLEMENTATION_CLAIM_URL=service_url,
        DISH_IMPLEMENTATION_CLAIM_TOKEN=ordinary_token,
        DISH_IMPLEMENTATION_CLAIM_RECOVERY_TOKEN=recovery_token,
        DISH_IMPLEMENTATION_CLAIM_ALLOW_HTTP="1",
    )

    try:
        first = run(
            [
                "python3", str(SCRIPT), "claim",
                "--task", task, "--branch", branch, "--agent-id", "a", "--base", base,
                "--", "python3", "-c", "pass",
            ],
            cwd=h.primary,
            env=parent_env,
            check=False,
        )
        assert first.returncode == 0, first.stderr

        claim_files = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}.json"))
        assert len(claim_files) == 1
        prior = json.loads(claim_files[0].read_text())

        child = """
import os
import sys
from implementation_claim_lib.client import ClaimServiceClient
from implementation_claim_lib.errors import ClaimError
assert 'DISH_IMPLEMENTATION_CLAIM_RECOVERY_TOKEN' not in os.environ
assert os.environ['DISH_IMPLEMENTATION_CLAIM_TOKEN'] == 'ordinary-service-token'
assert os.environ['DISH_IMPLEMENTATION_GLOBAL_WRITER_CAPABILITY']
client = ClaimServiceClient.from_env()
try:
    client.takeover(
        task_gid=os.environ['DISH_AGENT_CLAIM_TASK'],
        expected_claim_id=os.environ['DISH_IMPLEMENTATION_GLOBAL_CLAIM_ID'],
        owner='child-self-promote',
        session_id='child-session',
        host='child-host',
        authoring_base_sha=sys.argv[1],
        reason='child must not own recovery authority',
        liveness_evidence='ordinary claimed child',
    )
except ClaimError as exc:
    assert exc.code == 'RECOVERY_AUTHORITY_REQUIRED', exc
else:
    raise AssertionError('ordinary claimed child unexpectedly performed takeover')
"""
        second = run(
            [
                "python3", str(SCRIPT), "claim",
                "--task", task, "--branch", branch, "--agent-id", "b", "--base", base,
                "--takeover",
                "--expected-claim", str(prior["token"]),
                "--expected-global-claim", str(prior["global_claim_id"]),
                "--takeover-reason", "fixture authorized orchestration handoff",
                "--liveness-evidence", "fixture explicit recovery authority",
                "--", "python3", "-c", child, base,
            ],
            cwd=h.primary,
            env=parent_env,
            check=False,
        )
        assert second.returncode == 0, second.stderr

        replacement = json.loads(claim_files[0].read_text())
        second_claim_id = str(replacement["global_claim_id"])
        second_writer = str(replacement["global_writer_capability"])
        assert second_claim_id != str(prior["global_claim_id"])

        orchestrator = ClaimServiceClient(
            url=service_url,
            token=ordinary_token,
            repository="marcogallotta/ai-tools",
            recovery_token=recovery_token,
        )
        third = orchestrator.takeover(
            task_gid=task,
            expected_claim_id=second_claim_id,
            owner="orchestrator-replacement",
            session_id="orchestrator-session",
            host="orchestrator-host",
            authoring_base_sha=base,
            reason="separately authorized recovery path",
            liveness_evidence="explicit recovery credential remains outside child",
        )
        assert third["claim_id"] != second_claim_id
        assert third["writer_capability"] != second_writer
        with pytest.raises(ClaimError) as stale_writer:
            orchestrator.authorize(
                task_gid=task,
                claim_id=third["claim_id"],
                writer_capability=second_writer,
                branch=branch,
            )
        assert stale_writer.value.code == "WRITER_AUTHORITY_DENIED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
