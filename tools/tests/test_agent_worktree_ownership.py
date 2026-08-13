from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agent_worktree_support import SCRIPT, Harness, assert_error, git_out, h


def claim_cmd(
    h: Harness,
    *,
    task: str,
    branch: str,
    agent: str,
    child: list[str],
    takeover: bool = False,
    pr_number: int | None = None,
    pr_head: str | None = None,
    lease_state: str | None = None,
    lease_id: str | None = None,
) -> list[str]:
    args = [
        "python3", str(SCRIPT), "claim",
        "--task", task, "--branch", branch, "--agent-id", agent,
    ]
    if takeover:
        args.append("--takeover")
    if pr_number is not None:
        args += ["--pr-number", str(pr_number)]
    if pr_head is not None:
        args += ["--pr-head", pr_head]
    if lease_state is not None:
        args += ["--pr-lease-state", lease_state]
    if lease_id is not None:
        args += ["--pr-lease-id", lease_id]
    return [*args, "--", *child]


def run_claim(h: Harness, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        claim_cmd(h, **kwargs),
        cwd=h.primary,
        env=h.env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def claim_file(h: Harness, task: str) -> Path:
    matches = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}.json"))
    assert len(matches) == 1, matches
    return matches[0]


def wait_for_claim(h: Harness, task: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}.json"))
        if len(matches) == 1:
            record = json.loads(matches[0].read_text(encoding="utf-8"))
            if record.get("released_at") is None:
                return
        time.sleep(0.02)
    raise AssertionError("live claim did not appear")


def test_direct_unclaimed_start_is_refused_before_any_writer_state(h: Harness) -> None:
    task = "3000"
    branch = "agent/unclaimed"
    h.agent_file("unclaimed-agent")
    result = h.raw_tool(
        "start", "--task", task, "--branch", branch,
        "--base-ref", "refs/heads/main", "--base", h.current_remote_main(),
        "--agent-id", "unclaimed-agent", "--json", check=False,
    )
    assert_error(result, "OWNERSHIP_CLAIM_REQUIRED")
    assert not h.state_path(task).exists() and not h.wt(task).exists()


def test_two_simultaneous_claims_produce_exactly_one_live_owner(h: Harness) -> None:
    task = "3001"
    branch = "agent/claim-race"
    for agent in ("claim-a", "claim-b"):
        h.agent_file(agent)
    child = ["python3", "-c", "import time; time.sleep(0.8)"]
    p1 = subprocess.Popen(claim_cmd(h, task=task, branch=branch, agent="claim-a", child=child), cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(claim_cmd(h, task=task, branch=branch, agent="claim-b", child=child), cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    o1, e1 = p1.communicate(timeout=20)
    o2, e2 = p2.communicate(timeout=20)
    results = [(p1.returncode, o1, e1), (p2.returncode, o2, e2)]
    assert sum(code == 0 for code, _, _ in results) == 1, results
    loser = next(item for item in results if item[0] != 0)
    assert "ERROR OWNERSHIP_CLAIMED:" in loser[2]


def test_live_owner_collision_refuses_takeover(h: Harness) -> None:
    task = "3002"
    branch = "agent/live-owner"
    h.agent_file("owner-a")
    h.agent_file("owner-b")
    h.start(task=task, branch=branch, agent="owner-a")
    p = subprocess.Popen(
        claim_cmd(h, task=task, branch=branch, agent="owner-a", child=["python3", "-c", "import time; time.sleep(1.0)"]),
        cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    wait_for_claim(h, task)
    result = run_claim(h, task=task, branch=branch, agent="owner-b", takeover=True, child=["python3", "-c", "pass"])
    assert_error(result, "OWNERSHIP_CLAIMED")
    p.communicate(timeout=20)
    assert p.returncode == 0
    assert h.state(task)["owner"]["agent_id"] == "owner-a"


def test_stale_claim_recovery_requires_explicit_takeover(h: Harness) -> None:
    task = "3003"
    branch = "agent/stale-recovery"
    h.agent_file("stale-a")
    h.agent_file("stale-b")
    first = run_claim(h, task=task, branch=branch, agent="stale-a", child=["python3", "-c", "pass"])
    assert first.returncode == 0, first.stderr
    path = claim_file(h, task)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["released_at"] = None
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    refused = run_claim(h, task=task, branch=branch, agent="stale-b", child=["python3", "-c", "pass"])
    assert_error(refused, "OWNER_HANDOFF_REQUIRED")
    recovered = run_claim(h, task=task, branch=branch, agent="stale-b", takeover=True, child=["python3", "-c", "pass"])
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(path.read_text(encoding="utf-8"))["agent_id"] == "stale-b"


def test_existing_worktree_owner_outweighs_advisory_pr_lease_and_pr_identity_is_reconciled(h: Harness) -> None:
    task = "3004"
    branch = "agent/pr-reconcile"
    h.agent_file("lease-a")
    h.agent_file("lease-b")
    h.start(task=task, branch=branch, agent="lease-a")
    h.tool("publish", "--task", task, "--json")
    head = git_out(h.wt(task), "rev-parse", "HEAD")
    visible = run_claim(
        h, task=task, branch=branch, agent="lease-a", child=["python3", "-c", "pass"],
        pr_number=45, pr_head=head, lease_state="active", lease_id="lease-visible-a",
    )
    assert visible.returncode == 0, visible.stderr

    other = run_claim(
        h, task=task, branch=branch, agent="lease-b", child=["python3", "-c", "pass"],
        pr_number=45, pr_head=head, lease_state="active", lease_id="lease-visible-b",
    )
    assert_error(other, "OWNER_HANDOFF_REQUIRED")
    assert h.state(task)["owner"]["agent_id"] == "lease-a"

    conflicting_pr = run_claim(
        h, task=task, branch=branch, agent="lease-a", child=["python3", "-c", "pass"],
        pr_number=46, pr_head=head, lease_state="none",
    )
    assert_error(conflicting_pr, "OWNERSHIP_AMBIGUOUS")


def test_failed_claim_causes_zero_overlapping_writer_state(h: Harness) -> None:
    task = "3005"
    branch = "agent/no-overlap"
    h.agent_file("writer-a")
    h.agent_file("writer-b")
    h.start(task=task, branch=branch, agent="writer-a")
    sentinel = h.root / "second-writer-ran"
    p = subprocess.Popen(
        claim_cmd(h, task=task, branch=branch, agent="writer-a", child=["python3", "-c", "import time; time.sleep(1.0)"]),
        cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    wait_for_claim(h, task)
    result = run_claim(
        h, task=task, branch=branch, agent="writer-b", takeover=True,
        child=["python3", "-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text('overlap')"],
    )
    assert_error(result, "OWNERSHIP_CLAIMED")
    assert not sentinel.exists()
    assert h.state(task)["owner"]["agent_id"] == "writer-a"
    records = git_out(h.primary, "worktree", "list", "--porcelain").split("\n\n")
    assert sum(str(h.wt(task)) in record for record in records) == 1
    p.communicate(timeout=20)
    assert p.returncode == 0


def test_same_task_different_active_branch_pr_lineage_cannot_be_silently_adopted(h: Harness) -> None:
    task = "3006"
    branch_a = "agent/lineage-a"
    branch_b = "agent/lineage-b"
    h.agent_file("lineage-a-owner")
    h.agent_file("lineage-b-worker")
    h.start(task=task, branch=branch_a, agent="lineage-a-owner")
    sentinel = h.root / "wrong-lineage-ran"
    result = run_claim(
        h, task=task, branch=branch_b, agent="lineage-b-worker", takeover=True,
        child=["python3", "-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text('wrong')"],
    )
    assert_error(result, "OWNERSHIP_AMBIGUOUS")
    assert not sentinel.exists()
    assert h.state(task)["branch"] == branch_a


def test_repository_owned_implementation_handoff_contract_is_shared_by_dispatch_roles() -> None:
    repo = Path(__file__).resolve().parents[2]
    template = repo / "dish/docs/agents/templates/implementation-handoff.md"
    body = template.read_text(encoding="utf-8")
    for required in (
        "Repository:", "Asana task GID", "Authorized branch", "Base ref", "Base SHA",
        "Existing PR", "Expected PR head", "not authorization", "agent-worktree claim",
    ):
        assert required in body
    link = "templates/implementation-handoff.md"
    for relative in (
        "dish/docs/agents/coordinator.md",
        "dish/docs/agents/index.md",
        "dish/docs/agents/implementation.md",
    ):
        assert link in (repo / relative).read_text(encoding="utf-8")
    index = (repo / "dish/docs/agents/index.md").read_text(encoding="utf-8")
    assert "Coordinator, Development Workflow, and Implementation" in index


def test_claim_rejects_pr_head_that_does_not_match_authorized_remote_branch(h: Harness) -> None:
    task = "3007"
    branch = "agent/pr-head-bind"
    base = h.current_remote_main()
    remote_head = h.remote_branch_commit(branch, "remote pr head", start=base)
    assert remote_head != base
    h.agent_file("pr-head-worker")
    sentinel = h.root / "stale-pr-head-ran"
    result = run_claim(
        h, task=task, branch=branch, agent="pr-head-worker",
        pr_number=44, pr_head=base, lease_state="none",
        child=["python3", "-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')"],
    )
    assert_error(result, "PR_BRANCH_HEAD_MISMATCH")
    assert not sentinel.exists()
    assert not h.state_path(task).exists() and not h.wt(task).exists()
