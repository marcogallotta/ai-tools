from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agent_worktree_support import SCRIPT, Harness, assert_error, git_out, h


def claim(h: Harness, *, task: str, branch: str, agent: str, child: list[str], takeover=False, expected=None, pr=None, head=None):
    identity_path = h.home / ".local/state/dish/agents" / f"{agent}.json"
    if identity_path.exists():
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity.get("owning_task_gid") is None:
            identity["owning_task_gid"] = task
            identity_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
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
    h.agent_file("direct", owning_task_gid="3000")
    direct = h.raw_tool("start", "--task", "3000", "--branch", "agent/direct", "--base-ref", "refs/heads/main", "--base", h.current_remote_main(), "--agent-id", "direct", check=False)
    assert_error(direct, "OWNERSHIP_CLAIM_REQUIRED")
    for agent in ("a", "b"):
        h.agent_file(agent, owning_task_gid="3001")
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
    h.agent_file(agent, owning_task_gid=task)
    new_head = h.remote_branch_commit(
        "agent/head-move-fence-next", "published successor", start=head
    )
    tools_dir = Path(__file__).resolve().parents[1]
    child = f"""
import json
import os
import pathlib
import subprocess
import sys
sys.path.insert(0, {str(tools_dir)!r})
from agent_worktree_lib.common import AgentWorktreeError, GitRunner
from agent_worktree_lib.ownership import invalidate_claim_after_head_movement, require_active_claim

task = {task!r}
branch = {branch!r}
agent = {agent!r}
new_head = {new_head!r}
subprocess.run(
    ["git", "--git-dir=" + os.environ["TEST_BARE_ORIGIN"], "update-ref", "refs/heads/{branch}", new_head],
    check=True,
)
path = pathlib.Path.home() / "github-reviews.json"
data = json.loads(path.read_text())
data[{str(pr)!r}]["pr"]["head"]["sha"] = new_head
path.write_text(json.dumps(data) + "\\n")
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


def test_repository_claim_rejects_non_implementation_role(h: Harness) -> None:
    h.agent_file("reviewer", role="review", owning_task_gid="3010")
    result = claim(
        h, task="3010", branch="agent/non-implementation", agent="reviewer",
        child=["python3", "-c", "raise SystemExit('must not run')"],
    )
    assert_error(result, "MUTATION_AUTHORITY_REQUIRED")


def test_repository_claim_rejects_mismatched_task_identity(h: Harness) -> None:
    h.agent_file("impl", role="implementation", owning_task_gid="9999")
    result = claim(
        h, task="3011", branch="agent/wrong-task", agent="impl",
        child=["python3", "-c", "raise SystemExit('must not run')"],
    )
    assert_error(result, "MUTATION_AUTHORITY_TASK_MISMATCH")


def test_repository_claim_rejects_missing_task_identity(h: Harness) -> None:
    h.agent_file("impl", role="implementation")
    result = h.raw_tool(
        "claim", "--task", "3012", "--branch", "agent/missing-task", "--agent-id", "impl",
        "--", "python3", "-c", "raise SystemExit('must not run')",
        check=False,
    )
    assert_error(result, "MUTATION_AUTHORITY_TASK_REQUIRED")


def test_repository_claim_rejects_non_implementation_task_mode(h: Harness) -> None:
    h.agent_file("impl", role="implementation", owning_task_gid="3014")
    h.set_task_section("3014", "Needs Research")
    result = claim(
        h, task="3014", branch="agent/needs-research", agent="impl",
        child=["python3", "-c", "raise SystemExit('must not run')"],
    )
    assert_error(result, "MUTATION_TASK_MODE_BLOCKED")


def test_repository_writer_rechecks_role_after_claim(h: Harness) -> None:
    task, branch, agent = "3013", "agent/role-change", "impl"
    h.agent_file(agent, role="implementation", owning_task_gid=task)
    h.start(task=task, branch=branch, agent=agent)
    path = h.home / ".local/state/dish/agents" / f"{agent}.json"
    payload = json.loads(path.read_text())
    payload["role"] = "review"
    path.write_text(json.dumps(payload) + "\n")
    result = h.tool("commit", "--task", task, "--agent-id", agent, "--message", "x", "tracked.txt", check=False)
    assert_error(result, "MUTATION_AUTHORITY_REQUIRED")


def test_repository_writer_rechecks_current_task_mode_after_claim(h: Harness) -> None:
    task, branch, agent = "3015", "agent/mode-change", "impl"
    h.agent_file(agent, role="implementation", owning_task_gid=task)
    h.start(task=task, branch=branch, agent=agent)
    h.set_task_section(task, "Needs Research")
    result = h.tool("commit", "--task", task, "--agent-id", agent, "--message", "x", "tracked.txt", check=False)
    assert_error(result, "MUTATION_TASK_MODE_BLOCKED")


def test_registered_postgresql_ready_claim_requires_and_consumes_exact_local_handoff(h: Harness) -> None:
    task, branch, agent = "3110", "agent/postgresql-ready", "pg-impl"
    h.agent_file(agent, owning_task_gid=task)
    h.set_task_project(task, "1217404747383060", "Dish — PostgreSQL / Dark Launch v2")
    h.set_task_section(task, "Ready")

    admitted = claim(h, task=task, branch=branch, agent=agent, child=["python3", "-c", "pass"])
    assert admitted.returncode == 0, admitted.stderr
    _, payload = record(h, task)
    authority = payload["repository_assignment_authority"]
    assert authority["assignment"]["branch"] == branch
    assert authority["handoff_id"]


def test_registered_coordinator_under_development_is_project_agnostic(h: Harness) -> None:
    task, branch, agent = "3111", "agent/coordinator-v2", "coord-impl"
    h.agent_file(agent, owning_task_gid=task)
    h.set_task_project(task, "1217382473444945", "Dish — Coordinator v2")
    result = claim(h, task=task, branch=branch, agent=agent, child=["python3", "-c", "pass"])
    assert result.returncode == 0, result.stderr


def test_ready_claim_rejects_missing_duplicate_tampered_or_mismatched_handoff(h: Harness) -> None:
    cases = (
        ("3112", {"TEST_ASANA_NO_HANDOFF": "1"}),
        ("3113", {"TEST_ASANA_DUPLICATE_HANDOFF": "1"}),
        ("3114", {"TEST_ASANA_TAMPER_HANDOFF": "1"}),
        ("3115", {"TEST_ASANA_BRANCH": "agent/wrong-branch"}),
    )
    for task, extra_env in cases:
        agent, branch = f"impl-{task}", f"agent/ready-{task}"
        h.agent_file(agent, owning_task_gid=task)
        h.set_task_section(task, "Ready")
        result = h.raw_tool(
            "claim", "--task", task, "--branch", branch, "--agent-id", agent,
            "--", "python3", "-c", "raise SystemExit('must not run')",
            env=extra_env,
            check=False,
        )
        assert_error(result, "MUTATION_READY_HANDOFF_INVALID")


def test_repository_claim_rejects_zero_multiple_unregistered_and_bad_v2_modes(h: Harness) -> None:
    cases = []
    task = "3116"
    h.set_task_project(task, "9999999999999999", "Dish — Unregistered v2")
    cases.append((task, "MUTATION_TASK_AUTHORITY_INVALID"))
    task = "3117"
    h.set_task_projects(task, [
        {"gid": "1217419962189616", "name": "Dish — Development Workflow v2"},
        {"gid": "1217404747383060", "name": "Dish — PostgreSQL / Dark Launch v2"},
    ])
    cases.append((task, "MUTATION_TASK_AUTHORITY_INVALID"))
    task = "3118"
    h.set_task_project(task, "1217404747383060", "Dish — PostgreSQL / Dark Launch v3")
    cases.append((task, "MUTATION_TASK_MODE_UNSUPPORTED"))
    task = "3119"
    h.set_task_project(task, "1217404747383060", "Dish — PostgreSQL / Dark Launch v2")
    h.set_project_sections("1217404747383060", ["Ready", "Under Development"])
    cases.append((task, "MUTATION_TASK_MODE_CONTRADICTORY"))

    for task, expected in cases:
        agent, branch = f"impl-{task}", f"agent/mode-{task}"
        h.agent_file(agent, owning_task_gid=task)
        result = claim(
            h,
            task=task,
            branch=branch,
            agent=agent,
            child=["python3", "-c", "raise SystemExit('must not run')"],
        )
        assert_error(result, expected)


def test_registered_v2_writer_rejects_every_nonimplementation_lifecycle(h: Harness) -> None:
    blocked = (
        "Needs Processing", "Needs Research", "Needs Agentic Review", "Needs Human Review",
        "Waiting on Dependency", "Needs Post-Merge Rollout", "Done",
    )
    for index, section in enumerate(blocked, start=3120):
        task, branch, agent = str(index), f"agent/writer-{index}", f"impl-{index}"
        h.agent_file(agent, owning_task_gid=task)
        h.start(task=task, branch=branch, agent=agent)
        h.set_task_section(task, section)
        result = h.tool("commit", "--task", task, "--agent-id", agent, "--message", "x", "tracked.txt", check=False)
        assert_error(result, "MUTATION_TASK_MODE_BLOCKED")


def test_registered_v2_admission_is_read_only_and_registry_projection_matches_contract(h: Harness) -> None:
    task, branch, agent = "3130", "agent/read-only-admission", "impl-3130"
    h.agent_file(agent, owning_task_gid=task)
    h.set_task_project(task, "1217404747383060", "Dish — PostgreSQL / Dark Launch v2")
    h.set_task_section(task, "Ready")
    result = claim(h, task=task, branch=branch, agent=agent, child=["python3", "-c", "pass"])
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in h.asana_log.read_text(encoding="utf-8").splitlines()]
    assert calls
    assert all(call[:2] == ["raw", "GET"] for call in calls)

    repo = Path(__file__).resolve().parents[2]
    contract = (repo / "dish/docs/agents/asana-v2-project-mode.md").read_text(encoding="utf-8")
    import sys

    sys.path.insert(0, str(repo / "tools"))
    try:
        from agent_worktree_lib.asana_v2 import REGISTERED_V2_PROJECTS, V2_LIFECYCLE_SECTIONS
    finally:
        sys.path.pop(0)

    for gid, base_name in REGISTERED_V2_PROJECTS.items():
        assert f"| `{gid}` | {base_name} |" in contract
    assert len(V2_LIFECYCLE_SECTIONS) == 9


def test_writer_rejects_missing_or_tampered_persisted_handoff_authority(h: Harness) -> None:
    for index, mutation in enumerate(("missing", "branch"), start=3131):
        task, branch, agent = str(index), f"agent/authority-{index}", f"impl-{index}"
        h.agent_file(agent, owning_task_gid=task)
        h.start(task=task, branch=branch, agent=agent)
        state = h.state(task)
        if mutation == "missing":
            state["repository_assignment_authority"] = {}
        else:
            state["repository_assignment_authority"]["assignment"]["branch"] = "agent/tampered"
        h.state_path(task).write_text(json.dumps(state) + "\n", encoding="utf-8")
        result = h.tool(
            "commit", "--task", task, "--agent-id", agent,
            "--message", "must not commit", "tracked.txt", check=False,
        )
        assert_error(result, "MUTATION_ASSIGNMENT_MISMATCH")


def test_launch_provenance_with_pr_does_not_bypass_missing_ready_handoff(h: Harness) -> None:
    task, branch, agent, pr = "3133", "agent/no-block-bypass", "impl-3133", 3133
    h.agent_file(agent, owning_task_gid=task)
    h.set_task_section(task, "Ready")
    head = h.remote_branch_commit(branch, "non-BLOCK provenance candidate", start=h.current_remote_main())
    provenance = _write_launch_provenance(
        h, agent=agent, task=task, branch=branch, pr=pr, head=head
    )
    result = h.raw_tool(
        "claim", "--task", task, "--branch", branch, "--agent-id", agent,
        "--pr-number", str(pr), "--pr-head", head, "--pr-lease-state", "none",
        "--launch-provenance", str(provenance), "--require-launch-provenance",
        "--", "python3", "-c", "raise SystemExit('must not run')",
        env={"TEST_ASANA_NO_HANDOFF": "1"},
        check=False,
    )
    assert_error(result, "MUTATION_READY_HANDOFF_INVALID")


def test_exact_formal_block_review_authorizes_only_its_bound_pr_head(h: Harness) -> None:
    task, branch, agent, pr, review_id = "3134", "agent/exact-block", "impl-3134", 3134, "913134"
    h.set_task_section(task, "Ready")
    head = h.remote_branch_commit(branch, "blocked candidate", start=h.current_remote_main())
    h.set_block_review(task=task, pr=pr, branch=branch, head=head, review_id=review_id)
    provenance = _write_launch_provenance(
        h, agent=agent, task=task, branch=branch, pr=pr, head=head,
        block_review_id=review_id,
    )
    result = h.raw_tool(
        "claim", "--task", task, "--branch", branch, "--agent-id", agent,
        "--pr-number", str(pr), "--pr-head", head, "--pr-lease-state", "none",
        "--launch-provenance", str(provenance), "--require-launch-provenance",
        "--", "python3", "-c", "pass",
        env={"TEST_ASANA_NO_HANDOFF": "1"}, check=False,
    )
    assert result.returncode == 0, result.stderr
    _, claim_record = record(h, task)
    authority = claim_record["repository_assignment_authority"]
    assert authority["review_block_continuation"] is True
    assert authority["block_review_id"] == review_id
    assert authority["assignment"]["pr_head"] == head


def test_review_block_continuation_rejects_missing_or_wrong_formal_review(h: Harness) -> None:
    for index, verdict in enumerate((None, "MERGE"), start=3135):
        task, branch, agent, pr, review_id = str(index), f"agent/bad-block-{index}", f"impl-{index}", index, f"91{index}"
        h.set_task_section(task, "Ready")
        head = h.remote_branch_commit(branch, f"candidate {index}", start=h.current_remote_main())
        if verdict is not None:
            h.set_block_review(
                task=task, pr=pr, branch=branch, head=head,
                review_id=review_id, verdict=verdict,
            )
        provenance = _write_launch_provenance(
            h, agent=agent, task=task, branch=branch, pr=pr, head=head,
            block_review_id=review_id,
        )
        result = h.raw_tool(
            "claim", "--task", task, "--branch", branch, "--agent-id", agent,
            "--pr-number", str(pr), "--pr-head", head, "--pr-lease-state", "none",
            "--launch-provenance", str(provenance), "--require-launch-provenance",
            "--", "python3", "-c", "raise SystemExit('must not run')",
            env={"TEST_ASANA_NO_HANDOFF": "1"}, check=False,
        )
        expected = (
            "MUTATION_REVIEW_BLOCK_AUTHORITY_UNAVAILABLE"
            if verdict is None else "MUTATION_REVIEW_BLOCK_AUTHORITY_INVALID"
        )
        assert_error(result, expected)


def test_writer_rejects_claim_and_state_assignment_authority_disagreement(h: Harness) -> None:
    task, branch, agent = "3137", "agent/state-claim-drift", "impl-3137"
    h.agent_file(agent, owning_task_gid=task)
    h.start(task=task, branch=branch, agent=agent)
    state = h.state(task)
    state["repository_assignment_authority"]["assignment"]["branch"] = "agent/tampered"
    h.state_path(task).write_text(json.dumps(state) + "\n", encoding="utf-8")
    result = h.tool(
        "commit", "--task", task, "--agent-id", agent,
        "--message", "must not commit", "tracked.txt", check=False,
    )
    assert_error(result, "MUTATION_ASSIGNMENT_MISMATCH")


def test_first_pr_attachment_requires_fresh_exact_pr_head_handoff(h: Harness) -> None:
    task, branch, agent, pr = "3138", "agent/pr-attachment", "impl-3138", 3138
    h.agent_file(agent, owning_task_gid=task)
    h.start(task=task, branch=branch, agent=agent)
    h.tool("publish", "--task", task)
    head = git_out(h.wt(task), "rev-parse", "HEAD")
    result = h.raw_tool(
        "claim", "--task", task, "--branch", branch, "--agent-id", agent,
        "--pr-number", str(pr), "--pr-head", head, "--pr-lease-state", "none",
        "--", "python3", "-c", "raise SystemExit('must not run')",
        env={"TEST_ASANA_NO_HANDOFF": "1"}, check=False,
    )
    assert_error(result, "MUTATION_READY_HANDOFF_INVALID")


def test_live_pr_head_movement_blocks_writer_inside_already_admitted_claim(h: Harness) -> None:
    task, branch, agent, pr = "3140", "agent/live-head-drift", "impl-3140", 3140
    base = h.current_remote_main()
    admitted_head = h.remote_branch_commit(branch, "admitted PR head", start=base)
    h.agent_file(agent, owning_task_gid=task)
    h.tool(
        "adopt", "--task", task, "--branch", branch,
        "--base-ref", "refs/heads/main", "--base", base,
        "--expected-head", admitted_head, "--agent-id", agent, "--json",
    )
    moved_head = h.remote_branch_commit("agent/live-head-drift-next", "external PR move", start=admitted_head)
    before = git_out(h.wt(task), "rev-parse", "HEAD")
    mover = h.root / "move-pr-before-writer.py"
    mover.write_text(
        "import json, os, pathlib, subprocess, sys\n"
        f"subprocess.run(['git', '--git-dir=' + os.environ['TEST_BARE_ORIGIN'], 'update-ref', 'refs/heads/{branch}', '{moved_head}'], check=True)\n"
        "path=pathlib.Path.home()/'github-reviews.json'; data=json.loads(path.read_text())\n"
        f"data['{pr}']['pr']['head']['sha']='{moved_head}'; path.write_text(json.dumps(data)+'\\n')\n"
        f"raise SystemExit(subprocess.run(['python3', '{SCRIPT}', 'commit', '--task', '{task}', '-m', 'must not commit', '--', 'tracked.txt']).returncode)\n",
        encoding="utf-8",
    )
    result = h.raw_tool(
        "claim", "--task", task, "--branch", branch, "--agent-id", agent,
        "--pr-number", str(pr), "--pr-head", admitted_head, "--pr-lease-state", "none",
        "--", "python3", str(mover), check=False,
    )
    assert_error(result, "PR_HEAD_MOVED_REDISPATCH_REQUIRED")
    assert git_out(h.wt(task), "rev-parse", "HEAD") == before == admitted_head
