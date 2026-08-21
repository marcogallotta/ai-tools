from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agent_worktree_support import SCRIPT, TOOLS_DIR, Harness, assert_error, git, git_out, h, payload


def _claim(h: Harness, task: str, branch: str, agent: str, *, pr: int | None = None, head: str | None = None, child: list[str] | None = None):
    args = ["python3", str(SCRIPT), "claim", "--task", task, "--branch", branch, "--agent-id", agent]
    if pr is not None:
        args += ["--pr-number", str(pr), "--pr-head", str(head), "--pr-lease-state", "none"]
    args += ["--", *(child or ["python3", "-c", "pass"])]
    return subprocess.run(args, cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_same_task_different_branches_are_distinct_concurrent_lineages(h: Harness) -> None:
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task="4100", branch="agent/lineage-a", agent="a")
    h.start(task="4100", branch="agent/lineage-b", agent="b")
    a = h.state("4100", "agent/lineage-a")
    b = h.state("4100", "agent/lineage-b")
    assert a["lineage_id"] != b["lineage_id"]
    assert a["worktree_path"] != b["worktree_path"]
    status = payload(h.raw_tool("status", "--task", "4100", "--json"))
    assert status["lineage_count"] == 2
    assert {item["branch"] for item in status["lineages"]} == {"agent/lineage-a", "agent/lineage-b"}


def test_same_branch_is_repository_wide_single_lineage_across_tasks(h: Harness) -> None:
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task="4101", branch="agent/shared-name", agent="a")
    refused = h.start(task="4102", branch="agent/shared-name", agent="b", check=False)
    assert_error(refused, "BRANCH_LINEAGE_OWNED")
    assert not h.state_paths("4102")


def test_same_task_same_branch_second_agent_requires_exact_takeover(h: Harness) -> None:
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task="4103", branch="agent/one-writer", agent="a")
    refused = _claim(h, "4103", "agent/one-writer", "b")
    assert_error(refused, "OWNER_HANDOFF_REQUIRED")


def test_pr_identity_is_permanently_bound_to_one_branch_lineage(h: Harness) -> None:
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task="4104", branch="agent/pr-a", agent="a")
    head_a = h.commit_local(task="4104", text="a")
    h.tool("publish", "--task", "4104", "--json")
    h.start(task="4104", branch="agent/pr-b", agent="b")
    # Commit B using its exact worktree because task-only helper is intentionally ambiguous.
    wt_b = h.wt("4104", "agent/pr-b")
    h._identity(wt_b)
    (wt_b / "b.txt").write_text("b\n", encoding="utf-8")
    git(wt_b, "add", "b.txt"); git(wt_b, "commit", "-m", "b")
    head_b = git_out(wt_b, "rev-parse", "HEAD")
    claim_b_publish = ["python3", str(SCRIPT), "publish", "--task", "4104", "--json"]
    assert _claim(h, "4104", "agent/pr-b", "b", child=claim_b_publish).returncode == 0
    assert _claim(h, "4104", "agent/pr-a", "a", pr=77, head=head_a).returncode == 0
    refused = _claim(h, "4104", "agent/pr-b", "b", pr=77, head=head_b)
    assert_error(refused, "PR_LINEAGE_CONFLICT")


def test_terminal_cleanup_tombstones_before_delete_and_sibling_survives(h: Harness) -> None:
    for agent in ("a", "b"):
        h.agent_file(agent)
    task = "4105"
    h.start(task=task, branch="agent/cleanup-a", agent="a")
    head_a = h.commit_local(task=task, text="a")
    h.tool("publish", "--task", task, "--json")
    # h.seed never touched agent/cleanup-a directly; fetch the object now so it
    # remains available locally for the later out-of-band resurrection push,
    # even after cleanup deletes the remote branch ref.
    git(h.seed, "fetch", "origin", f"refs/heads/agent/cleanup-a:refs/dish-test-import/cleanup-a")
    h.start(task=task, branch="agent/cleanup-b", agent="b")
    b_before = h.state(task, "agent/cleanup-b")
    cleaned = h.raw_tool(
        "cleanup", "--task", task, "--branch", "agent/cleanup-a", "--expected-head", head_a,
        "--pr-number", "88", "--disposition", "abandoned", "--json",
    )
    assert payload(cleaned)["remote_branch_removed"] is True
    assert h.state(task, "agent/cleanup-b") == b_before
    refused = h.start(task="4106", branch="agent/cleanup-a", agent="a", check=False)
    assert_error(refused, "BRANCH_NAME_RETIRED")
    # Out-of-band ref resurrection at the same terminal SHA creates no new agent authority.
    git(h.seed, "push", "origin", f"{head_a}:refs/heads/agent/cleanup-a")
    stale = _claim(h, task, "agent/cleanup-a", "a")
    assert_error(stale, "BRANCH_NAME_RETIRED")
    again = h.start(task="4106", branch="agent/cleanup-a", agent="a", check=False)
    assert_error(again, "BRANCH_NAME_RETIRED")


def test_published_branch_disappearance_fences_only_that_lineage(h: Harness) -> None:
    for agent in ("a", "b"):
        h.agent_file(agent)
    task = "4107"
    h.start(task=task, branch="agent/fence-a", agent="a")
    h.commit_local(task=task, text="a")
    h.tool("publish", "--task", task, "--json")
    h.start(task=task, branch="agent/fence-b", agent="b")
    git(h.seed, "push", "origin", ":refs/heads/agent/fence-a")
    refused = _claim(h, task, "agent/fence-a", "a")
    assert_error(refused, "LINEAGE_REMOTE_CONTRADICTION")
    assert _claim(h, task, "agent/fence-b", "b").returncode == 0


def test_branch_advance_during_admission_window_fences_the_new_lineage_and_fails_closed(
    h: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every writer command reaches lineage admission by running fully inside the
    # test harness's sandboxed environment (fake GIT_SSH_COMMAND redirecting to
    # a local bare origin, sandboxed HOME/worktree root). Calling the admission
    # function directly in-process below is deliberately routed through that
    # exact same sandbox. Subprocess children spawned with env=None inherit the
    # real OS-level environment, not a swapped os.environ object, so each var is
    # set individually here (monkeypatch.setenv/delenv go through os.putenv) --
    # so no git operation here can reach a real remote.
    for key, value in h.env.items():
        monkeypatch.setenv(key, value)
    for key in list(os.environ):
        if key not in h.env:
            monkeypatch.delenv(key, raising=False)
    sys.path.insert(0, str(TOOLS_DIR))
    from agent_worktree_lib.common import AgentWorktreeError, GitRunner
    from agent_worktree_lib.repository import discover_repository
    from agent_worktree_lib import lineage as lineage_mod

    branch = "agent/admission-race"
    base = h.current_remote_main()
    head_a = h.remote_branch_commit(branch, "first", start=base)

    runner = GitRunner()
    repo = discover_repository(runner, h.primary)

    real_push_cas = lineage_mod._push_marker_cas
    raced = False

    def racing_push_cas(runner, repo, *, branch, marker_sha, expected_sha):
        # Perform the real admission CAS (binding the marker to head_a), then --
        # before resolve_lineage_for_branch's own post-admission reread runs --
        # simulate a concurrent writer advancing the branch during this exact
        # admission window: the race the review flagged.
        nonlocal raced
        real_push_cas(runner, repo, branch=branch, marker_sha=marker_sha, expected_sha=expected_sha)
        if not raced:
            raced = True
            h.remote_branch_commit(branch, "raced forward", start=head_a)

    monkeypatch.setattr(lineage_mod, "_push_marker_cas", racing_push_cas)
    try:
        with pytest.raises(AgentWorktreeError) as excinfo:
            lineage_mod.resolve_lineage_for_branch(runner, repo, task_gid="4200", branch=branch)
    finally:
        monkeypatch.setattr(lineage_mod, "_push_marker_cas", real_push_cas)
    assert excinfo.value.code == "LINEAGE_ADMISSION_RACE"
    assert raced

    registry = lineage_mod.read_registry(runner, repo, branch)
    assert registry is not None
    _, marker = registry
    assert marker["state"] == "TERMINAL"
    assert marker["disposition"] == "admission-window-race"

    # The partially admitted lineage is fenced, not merely refused: the branch
    # name is now permanently retired, exactly like any other terminal lineage.
    h.agent_file("race-agent")
    again = _claim(h, "4200", branch, "race-agent")
    assert_error(again, "BRANCH_NAME_RETIRED")


def test_ordinary_fast_forward_after_established_admission_is_not_an_admission_race(
    h: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Companion to the admission-window race test above: a fast-forward that
    # happens after a lineage is already established (post-first-admission) is
    # ordinary drift and must still resolve, not fence -- only the first
    # admission call itself is held to the strict exact-match requirement.
    for agent in ("a",):
        h.agent_file(agent)
    task = "4201"
    branch = "agent/post-admission-drift"
    h.start(task=task, branch=branch, agent="a")
    h.commit_local(task=task, text="a")
    h.tool("publish", "--task", task, "--json")
    published_head = git_out(h.origin, "rev-parse", f"refs/heads/{branch}")
    h.remote_branch_commit(branch, "external fast-forward", start=published_head)
    result = _claim(h, task, branch, "a")
    assert result.returncode == 0, result.stderr


def test_task_only_mutation_is_ambiguous_when_multiple_lineages_exist(h: Harness) -> None:
    for agent in ("a", "b"):
        h.agent_file(agent)
    h.start(task="4108", branch="agent/amb-a", agent="a")
    h.start(task="4108", branch="agent/amb-b", agent="b")
    result = h.raw_tool("commit", "--task", "4108", "-m", "nope", "--", "tracked.txt", check=False)
    assert_error(result, "LINEAGE_AMBIGUOUS")



def test_different_tasks_same_branch_simultaneous_admission_has_one_winner(h: Harness) -> None:
    branch = "agent/cross-task-race"
    base = h.base
    processes = []
    for task, agent in (("4110", "a"), ("4111", "b")):
        h.agent_file(agent)
        start = [
            "python3", str(SCRIPT), "start", "--task", task, "--branch", branch,
            "--base-ref", "refs/heads/main", "--base", base, "--agent-id", agent, "--json",
        ]
        processes.append(subprocess.Popen(
            ["python3", str(SCRIPT), "claim", "--task", task, "--branch", branch, "--agent-id", agent, "--", *start],
            cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))
    results = [process.communicate(timeout=90) + (process.returncode,) for process in processes]
    assert sum(code == 0 for _, _, code in results) == 1, results
    loser_err = next(stderr for _, stderr, code in results if code != 0)
    assert "BRANCH_ADMISSION_RACE" in loser_err or "BRANCH_LINEAGE_OWNED" in loser_err


def test_first_publish_rejects_out_of_band_same_sha_remote_creation(h: Harness) -> None:
    h.agent_file("a")
    task, branch = "4112", "agent/first-publish-race"
    h.start(task=task, branch=branch, agent="a")
    state = h.state(task, branch)
    # Another actor creates the remote ref at the exact same SHA after lineage admission.
    git(h.seed, "push", "origin", f"{state['local_head']}:refs/heads/{branch}")
    refused = h.tool("publish", "--task", task, "--json", check=False)
    assert_error(refused, "BRANCH_CREATE_RACE")
    after = h.state(task, branch)
    assert after["published_head"] is None
    assert after["remote_owned_head"] is None

def test_legacy_single_state_is_migrated_conservatively_to_registry_lineage(h: Harness) -> None:
    h.agent_file("a")
    task, branch = "4109", "agent/legacy"
    h.start(task=task, branch=branch, agent="a")
    state_path = h.state_path(task, branch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    lineage_id = state.pop("lineage_id")
    legacy = h.home / ".local/state/dish/worktrees" / f"{task}.json"
    legacy.write_text(json.dumps(state) + "\n", encoding="utf-8")
    state_path.unlink()
    assert _claim(h, task, branch, "a", child=["python3", str(SCRIPT), "resume", "--task", task, "--agent-id", "a"]).returncode == 0
    migrated = h.state(task, branch)
    assert migrated["lineage_id"] == lineage_id
    assert not legacy.exists()
