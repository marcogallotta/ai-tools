from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agent_worktree_support import SCRIPT, Harness, assert_error, git_out, h


def claim(h: Harness, *, task: str, branch: str, agent: str, child: list[str], takeover=False, expected=None, pr=None, head=None):
    args = ["python3", str(SCRIPT), "claim", "--task", task, "--branch", branch, "--agent-id", agent, "--base", h.current_remote_main()]
    prior = None
    matches = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}.json"))
    if len(matches) == 1:
        prior = json.loads(matches[0].read_text())
    if takeover:
        args.append("--takeover")
        if prior is not None and prior.get("global_claim_id"):
            args += [
                "--expected-global-claim", str(prior["global_claim_id"]),
                "--takeover-reason", "fixture explicit handoff",
                "--liveness-evidence", "fixture prior owner declared stale",
            ]
    if expected is not None:
        args += ["--expected-claim", expected]
    if pr is not None:
        args += ["--pr-number", str(pr), "--pr-head", head, "--pr-lease-state", "none"]
    return subprocess.run([*args, "--", *child], cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def record(h: Harness, task: str) -> tuple[Path, dict[str, object]]:
    matches = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}.json"))
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
        stdout, stderr = process.communicate(timeout=20)
        results.append((process.returncode, stdout, stderr))
    assert sum(code == 0 for code, _, _ in results) == 1
    losing = next(err for code, _, err in results if code != 0)
    assert any(code in losing for code in ("OWNERSHIP_CONFLICT", "OWNERSHIP_CLAIMED", "OWNER_HANDOFF_REQUIRED"))


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
    p.communicate(timeout=20)


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


def test_two_local_processes_sharing_winning_global_claim_are_os_fenced(h: Harness) -> None:
    task, branch, agent = "3006", "agent/shared-global", "a"
    h.agent_file(agent)
    h.start(task=task, branch=branch, agent=agent)
    _, record_data = record(h, task)
    global_id = str(record_data["global_claim_id"])
    base = h.current_remote_main()
    long = subprocess.Popen(
        [
            "python3", str(SCRIPT), "claim", "--task", task, "--branch", branch, "--agent-id", agent,
            "--base", base, "--global-claim-id", global_id, "--",
            "python3", "-c", "import time; time.sleep(1)",
        ],
        cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, current = record(h, task)
        if current["released_at"] is None:
            break
        time.sleep(.02)
    second = subprocess.run(
        [
            "python3", str(SCRIPT), "claim", "--task", task, "--branch", branch, "--agent-id", agent,
            "--base", base, "--global-claim-id", global_id, "--", "python3", "-c", "pass",
        ],
        cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert_error(second, "OWNERSHIP_CLAIMED")
    long.communicate(timeout=20)
