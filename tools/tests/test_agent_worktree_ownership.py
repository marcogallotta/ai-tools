from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agent_worktree_support import SCRIPT, Harness, assert_error, git_out, h


def claim(h: Harness, *, task: str, branch: str, agent: str, child: list[str], takeover=False, expected=None, pr=None, head=None):
    args = ["python3", str(SCRIPT), "claim", "--task", task, "--branch", branch, "--agent-id", agent]
    if takeover:
        args.append("--takeover")
    if expected is not None:
        args += ["--expected-claim", expected]
    if pr is not None:
        args += ["--pr-number", str(pr), "--pr-head", head, "--pr-lease-state", "none"]
    return subprocess.run([*args, "--", *child], cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def record(h: Harness, task: str) -> tuple[Path, dict[str, object]]:
    matches = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}*.json"))
    assert len(matches) == 1
    return matches[0], json.loads(matches[0].read_text())


def test_claim_gate_and_concurrent_start_choose_one_owner(h: Harness) -> None:
    h.agent_file("direct")
    direct = h.raw_tool("start", "--task", "3000", "--branch", "agent/direct", "--base-ref", "refs/heads/main", "--base", h.current_remote_main(), "--agent-id", "direct", check=False)
    assert_error(direct, "OWNERSHIP_CLAIM_REQUIRED")
    for agent in ("a", "b"):
        h.agent_file(agent)
    base = h.current_remote_main()
    ps = []
    for agent in ("a", "b"):
        start_argv = [
            "python3", str(SCRIPT), "start",
            "--task", "3001", "--branch", "agent/race",
            "--base-ref", "refs/heads/main", "--base", base,
            "--agent-id", agent,
        ]
        child = [
            "python3", "-c",
            "import subprocess,time,sys; subprocess.check_call(sys.argv[1:]); time.sleep(.5)",
            *start_argv,
        ]
        ps.append(
            subprocess.Popen(
                ["python3", str(SCRIPT), "claim", "--task", "3001", "--branch", "agent/race", "--agent-id", agent, "--", *child],
                cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        )
    results = []
    for process in ps:
        stdout, stderr = process.communicate(timeout=60)
        results.append((process.returncode, stdout, stderr))
    assert sum(code == 0 for code, _, _ in results) == 1
    assert any(code != 0 and ("BRANCH_ADMISSION_RACE" in err or "OWNERSHIP_CLAIMED" in err) for code, _, err in results)


def test_live_owner_fences_takeover_and_second_writer(h: Harness) -> None:
    task, branch = "3002", "agent/live"
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task=task, branch=branch, agent="a")
    p = subprocess.Popen(["python3", str(SCRIPT), "claim", "--task", task, "--branch", branch, "--agent-id", "a", "--", "python3", "-c", "import time; time.sleep(1)"], cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, r = record(h, task)
        if r["released_at"] is None:
            break
        time.sleep(.02)
    sentinel = h.root / "overlap"
    _, r = record(h, task)
    result = claim(h, task=task, branch=branch, agent="b", takeover=True, expected=str(r["token"]), child=["python3", "-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text('x')"])
    assert_error(result, "OWNERSHIP_CLAIMED")
    assert not sentinel.exists()
    p.communicate(timeout=60)


def test_stale_takeover_is_exact_cas_and_aba_safe(h: Harness) -> None:
    task, branch = "3003", "agent/stale"
    for agent in ("a", "b", "c"):
        h.agent_file(agent)
    assert claim(h, task=task, branch=branch, agent="a", child=["python3", "-c", "pass"]).returncode == 0
    path, r = record(h, task)
    first = str(r["token"])
    r["released_at"] = None
    path.write_text(json.dumps(r) + "\n")
    assert_error(claim(h, task=task, branch=branch, agent="b", takeover=True, child=["python3", "-c", "pass"]), "EXPECTED_CLAIM_REQUIRED")
    assert_error(claim(h, task=task, branch=branch, agent="b", takeover=True, expected="0" * 32, child=["python3", "-c", "pass"]), "OWNER_CLAIM_CHANGED")
    assert claim(h, task=task, branch=branch, agent="b", takeover=True, expected=first, child=["python3", "-c", "pass"]).returncode == 0
    second = str(record(h, task)[1]["token"])
    assert second != first
    assert_error(claim(h, task=task, branch=branch, agent="c", takeover=True, expected=first, child=["python3", "-c", "pass"]), "OWNER_CLAIM_CHANGED")
    assert record(h, task)[1]["token"] == second


def test_failed_takeover_child_restores_prior_coherent_owner(h: Harness) -> None:
    task, branch = "3006", "agent/failed-takeover"
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task=task, branch=branch, agent="a")
    _, prior = record(h, task)
    prior_token = str(prior["token"])

    failed = claim(
        h,
        task=task,
        branch=branch,
        agent="b",
        takeover=True,
        expected=prior_token,
        child=["definitely-not-an-agent-worktree-command"],
    )
    assert_error(failed, "CLAIM_COMMAND_FAILED")
    _, after = record(h, task)
    assert after["agent_id"] == "a"
    assert after["token"] == prior_token
    assert h.state(task)["owner"]["agent_id"] == "a"

    resumed = claim(
        h,
        task=task,
        branch=branch,
        agent="a",
        child=["python3", str(SCRIPT), "resume", "--task", task, "--agent-id", "a"],
    )
    assert resumed.returncode == 0


def test_failed_wrapper_after_durable_takeover_keeps_new_owner_coherent(h: Harness) -> None:
    task, branch = "3007", "agent/takeover-then-fail"
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task=task, branch=branch, agent="a")
    prior_token = str(record(h, task)[1]["token"])
    child = [
        "python3",
        "-c",
        (
            "import subprocess,sys; "
            "subprocess.check_call(sys.argv[1:]); "
            "raise SystemExit(9)"
        ),
        "python3",
        str(SCRIPT),
        "resume",
        "--task",
        task,
        "--agent-id",
        "b",
        "--takeover",
    ]
    failed = claim(
        h,
        task=task,
        branch=branch,
        agent="b",
        takeover=True,
        expected=prior_token,
        child=child,
    )
    assert failed.returncode == 9
    _, after = record(h, task)
    assert after["agent_id"] == "b"
    assert after["released_at"] is not None
    assert h.state(task)["owner"]["agent_id"] == "b"
    assert claim(h, task=task, branch=branch, agent="b", child=["python3", "-c", "pass"]).returncode == 0


def test_inconsistent_stale_claim_has_bounded_tool_native_recovery(h: Harness) -> None:
    task, branch = "3008", "agent/recover-claim"
    for agent in ("a", "b", "c"):
        h.agent_file(agent)
    h.start(task=task, branch=branch, agent="a")
    path, stale = record(h, task)
    stale["agent_id"] = "b"
    stale["released_at"] = None
    path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    stale_token = str(stale["token"])

    refused = claim(h, task=task, branch=branch, agent="c", child=["python3", "-c", "pass"])
    assert_error(refused, "OWNERSHIP_AMBIGUOUS")
    assert f"claim --takeover --expected-claim {stale_token}" in refused.stderr
    assert h.state(task)["owner"]["agent_id"] == "a"

    recovered = claim(
        h,
        task=task,
        branch=branch,
        agent="b",
        takeover=True,
        expected=stale_token,
        child=["python3", str(SCRIPT), "resume", "--task", task, "--agent-id", "b", "--takeover"],
    )
    assert recovered.returncode == 0
    _, after = record(h, task)
    assert after["agent_id"] == "b"
    assert after["released_at"] is not None
    assert h.state(task)["owner"]["agent_id"] == "b"


def test_crash_boundary_survivors_remain_recoverable(h: Harness) -> None:
    task, branch = "3009", "agent/crash-boundaries"
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task=task, branch=branch, agent="a")
    path, stale = record(h, task)

    # Surviving state after process death immediately after the new claim
    # record was persisted: claim says b, durable task owner still says a.
    stale["agent_id"] = "b"
    stale["released_at"] = None
    path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    token = str(stale["token"])
    recovered = claim(
        h,
        task=task,
        branch=branch,
        agent="b",
        takeover=True,
        expected=token,
        child=["python3", str(SCRIPT), "resume", "--task", task, "--agent-id", "b", "--takeover"],
    )
    assert recovered.returncode == 0

    # Surviving state after durable owner transition but before claim release:
    # both records name b, while the now-dead process no longer holds locks.
    path, stale = record(h, task)
    stale["released_at"] = None
    path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    assert claim(h, task=task, branch=branch, agent="b", child=["python3", "-c", "pass"]).returncode == 0


def test_status_pr_visibility_and_cross_lineage_fail_closed(h: Harness) -> None:
    task, branch = "3004", "agent/owned"
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task=task, branch=branch, agent="a")
    status = json.loads(h.raw_tool("status", "--task", task, "--json").stdout)
    assert status["claim"]["claim_id"] == record(h, task)[1]["token"]
    h.tool("publish", "--task", task)
    head = git_out(h.wt(task), "rev-parse", "HEAD")
    assert claim(h, task=task, branch=branch, agent="a", pr=45, head=head, child=["python3", "-c", "pass"]).returncode == 0
    assert_error(claim(h, task=task, branch=branch, agent="b", pr=45, head=head, child=["python3", "-c", "pass"]), "OWNER_HANDOFF_REQUIRED")
    assert_error(claim(h, task=task, branch="agent/other", agent="b", takeover=True, expected=str(record(h, task)[1]["token"]), child=["python3", "-c", "pass"]), "OWNERSHIP_AMBIGUOUS")


def test_canonical_handoff_is_single_shared_source_and_matches_cli() -> None:
    repo = Path(__file__).resolve().parents[2]
    body = (repo / "dish/docs/agents/templates/implementation-handoff.md").read_text()
    legacy = repo / "ci/implementation-handoff.md"
    runbook = (repo / "ci/pr-lifecycle-dispatcher-runbook.md").read_text()
    for token in ("Repository:", "Asana task GID", "Authorized branch", "Base ref", "Base SHA", "Existing PR", "Expected PR head", "--expected-claim", "legacy-unclaimed"):
        assert token in body
    assert not legacy.exists()
    assert "dish/docs/agents/templates/implementation-handoff.md" in runbook
    assert "ci/implementation-handoff.md" not in runbook
    for path in ("coordinator.md", "development-workflow.md", "implementation.md", "index.md"):
        assert "templates/implementation-handoff.md" in (repo / "dish/docs/agents" / path).read_text()
    cli = (repo / "tools/agent_worktree_lib/cli.py").read_text()
    assert 'claim.add_argument("--expected-claim"' in cli
    assert 'sub.add_parser("bind-pr"' not in cli


def test_pr_head_must_match_authorized_remote_branch(h: Harness) -> None:
    base = h.current_remote_main()
    remote = h.remote_branch_commit("agent/pr", "remote head", start=base)
    assert remote != base
    h.agent_file("a")
    result = claim(h, task="3005", branch="agent/pr", agent="a", pr=44, head=base, child=["python3", "-c", "pass"])
    assert_error(result, "PR_BRANCH_HEAD_MISMATCH")
    assert not h.state_path("3005").exists()


def test_local_implementation_handoff_contract_is_terse_executable_and_pr_durable() -> None:
    repo = Path(__file__).resolve().parents[2]
    handoff = (repo / "tools/agent-worktree-handoff.md").read_text()
    root = (repo / "CLAUDE.md").read_text()

    assert "tools/agent-worktree-handoff.md" in root
    for token in (
        "use exactly two lines: `Blocker:`",
        "`Action:` giving one exact next action",
        "PostgreSQL bootstrap, package/service setup",
        "/tmp/dish-pg-bootstrap.sh",
        "set -euo pipefail",
        "/tmp/dish-pg-bootstrap.json",
        "Persist failure diagnostics as well as success evidence",
        "exactly one runnable command",
        "sudo bash /tmp/dish-pg-bootstrap.sh",
        "read the persisted output file",
        "Do not request authorization that the current task or standing role contract already grants",
        "Keep it current proactively",
        "must be durable on the PR before it is reported to Marco",
        "exact current remote PR head SHA",
        "exact local unpublished head SHA",
        "publication action and authorized fallback attempted",
        "persisted evidence/output-file path",
        "Branch/head publication and PR discussion are separate transports",
        "Never leave critical implementation or blocker state only in chat",
        "return only PR number, exact current PR head SHA, PASS/FAIL, and next action",
    ):
        assert token in handoff


def _write_launch_provenance(
    h: Harness,
    *,
    agent: str,
    task: str,
    branch: str,
    pr: int,
    head: str,
    host: str = "claude",
    project: str = "1217419962189616",
    issued_at: str | None = None,
    **overrides,
) -> Path:
    import datetime as dt

    root = h.home / ".local/state/dish/launch-provenance"
    root.mkdir(parents=True, exist_ok=True)
    launch_id = f"launch-{agent}-{task}"
    payload = {
        "schema": "dish-local-implementation-launch-v1",
        "repository": "marcogallotta/ai-tools",
        "agent_id": agent,
        "host": host,
        "identity_source": "codex_thread_id" if host == "codex" else "claude_session_id",
        "role": "implementation",
        "task_gid": task,
        "owning_project_gid": project,
        "branch": branch,
        "pr_number": pr,
        "pr_head": head,
        "workspace": str(h.primary),
        "launcher": "fixture-local-implementation-launcher",
        "launch_id": launch_id,
        "issued_at": issued_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    payload.update(overrides)
    path = root / f"{launch_id}.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_claim_can_bind_missing_identity_only_from_exact_launch_provenance(h: Harness) -> None:
    task, branch, agent, pr = "3090", "agent/provenance-bind", "fresh-session", 91
    head = h.remote_branch_commit(branch, "provenance candidate", start=h.current_remote_main())
    provenance = _write_launch_provenance(h, agent=agent, task=task, branch=branch, pr=pr, head=head)

    result = h.raw_tool(
        "claim",
        "--task", task,
        "--branch", branch,
        "--agent-id", agent,
        "--launch-provenance", str(provenance),
        "--require-launch-provenance",
        "--pr-number", str(pr),
        "--pr-head", head,
        "--pr-lease-state", "none",
        "--",
        "python3", "-c", "pass",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    identity = json.loads((h.home / f".local/state/dish/agents/{agent}.json").read_text())
    assert identity["agent_id"] == agent
    assert identity["role"] == "implementation"
    assert identity["owning_task_gid"] == task
    assert identity["launch_provenance"]["pr_head"] == head
    assert identity["launch_provenance"]["identity_source"] == "claude_session_id"
    _, claim_record = record(h, task)
    assert claim_record["launch_provenance_required"] is True


def test_claim_required_launch_provenance_missing_fails_without_identity(h: Harness) -> None:
    task, branch, agent, pr = "3091", "agent/provenance-missing", "unknown-session", 92
    head = h.remote_branch_commit(branch, "missing provenance candidate", start=h.current_remote_main())
    result = h.raw_tool(
        "claim",
        "--task", task,
        "--branch", branch,
        "--agent-id", agent,
        "--require-launch-provenance",
        "--pr-number", str(pr),
        "--pr-head", head,
        "--pr-lease-state", "none",
        "--",
        "python3", "-c", "pass",
        check=False,
    )
    assert_error(result, "LAUNCH_PROVENANCE_REQUIRED")
    assert not (h.home / f".local/state/dish/agents/{agent}.json").exists()


def test_launch_provenance_shell_like_or_malformed_authority_fails_closed_without_execution(h: Harness) -> None:
    task, branch, agent, pr = "3092", "agent/provenance-shell", "shell-safe", 93
    head = h.remote_branch_commit(branch, "shell provenance candidate", start=h.current_remote_main())
    sentinel = h.root / "must-not-exist"
    provenance = _write_launch_provenance(
        h,
        agent=agent,
        task=task,
        branch=branch,
        pr=pr,
        head=head,
        project="${PROJECT_GID}",
        launcher=f"$(touch {sentinel})",
    )
    result = h.raw_tool(
        "claim",
        "--task", task,
        "--branch", branch,
        "--agent-id", agent,
        "--launch-provenance", str(provenance),
        "--require-launch-provenance",
        "--pr-number", str(pr),
        "--pr-head", head,
        "--pr-lease-state", "none",
        "--",
        "python3", "-c", "pass",
        check=False,
    )
    assert_error(result, "LAUNCH_PROVENANCE_INVALID")
    assert not sentinel.exists()
    assert not (h.home / f".local/state/dish/agents/{agent}.json").exists()


def test_stale_launch_provenance_fails_before_identity_or_claim(h: Harness) -> None:
    import datetime as dt

    task, branch, agent, pr = "3093", "agent/provenance-stale", "stale-session", 94
    head = h.remote_branch_commit(branch, "stale provenance candidate", start=h.current_remote_main())
    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    provenance = _write_launch_provenance(
        h, agent=agent, task=task, branch=branch, pr=pr, head=head, issued_at=stale
    )
    result = h.raw_tool(
        "claim",
        "--task", task,
        "--branch", branch,
        "--agent-id", agent,
        "--launch-provenance", str(provenance),
        "--require-launch-provenance",
        "--pr-number", str(pr),
        "--pr-head", head,
        "--pr-lease-state", "none",
        "--",
        "python3", "-c", "pass",
        check=False,
    )
    assert_error(result, "LAUNCH_PROVENANCE_STALE")
    assert not (h.home / f".local/state/dish/agents/{agent}.json").exists()


def test_failed_claim_child_restores_launch_identity_when_no_durable_owner(h: Harness) -> None:
    task, branch, agent, pr = "3094", "agent/provenance-child-fail", "failed-session", 95
    head = h.remote_branch_commit(branch, "failed provenance candidate", start=h.current_remote_main())
    provenance = _write_launch_provenance(h, agent=agent, task=task, branch=branch, pr=pr, head=head)

    result = h.raw_tool(
        "claim",
        "--task", task,
        "--branch", branch,
        "--agent-id", agent,
        "--launch-provenance", str(provenance),
        "--require-launch-provenance",
        "--pr-number", str(pr),
        "--pr-head", head,
        "--pr-lease-state", "none",
        "--",
        "python3", "-c", "raise SystemExit(7)",
        check=False,
    )

    assert result.returncode == 7
    assert not (h.home / f".local/state/dish/agents/{agent}.json").exists()
    assert not h.state_path(task).exists()
    assert not list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}*.json"))



def test_head_movement_invalidation_closes_semantic_mutation_but_allows_readback(h: Harness) -> None:
    task, branch, agent, pr = "3095", "agent/head-move-fence", "head-move-agent", 96
    head = h.remote_branch_commit(branch, "head move candidate", start=h.current_remote_main())
    h.agent_file(agent)
    new_head = "f" * 40
    tools_dir = Path(__file__).resolve().parents[1]
    child = f"""
import sys
sys.path.insert(0, {str(tools_dir)!r})
from agent_worktree_lib.common import AgentWorktreeError, GitRunner
from agent_worktree_lib.ownership import invalidate_claim_after_head_movement, require_active_claim

task = {task!r}
branch = {branch!r}
agent = {agent!r}
new_head = {new_head!r}
runner = GitRunner()
assert invalidate_claim_after_head_movement(task, new_head) is True
try:
    require_active_claim(task, branch, agent, runner)
except AgentWorktreeError as exc:
    assert exc.code == "PR_HEAD_MOVED_REDISPATCH_REQUIRED"
else:
    raise AssertionError("semantic mutation remained open after PR head movement")
assert require_active_claim(task, branch, agent, runner, allow_head_moved_readback=True)["head_moved_to"] == new_head
"""
    result = h.raw_tool(
        "claim", "--task", task, "--branch", branch, "--agent-id", agent,
        "--pr-number", str(pr), "--pr-head", head, "--pr-lease-state", "none",
        "--", "python3", "-c", child,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    _, claim = record(h, task)
    assert claim["head_moved_to"] == new_head
    assert claim["semantic_mutation_closed_at"]
