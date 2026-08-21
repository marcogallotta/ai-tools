from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import pytest

from agent_worktree_support import SCRIPT, TOOLS_DIR, Harness, assert_error, git, git_out, h

sys.path.insert(0, str(TOOLS_DIR))
from agent_worktree_lib.common import AgentWorktreeError, GitRunner
from agent_worktree_lib import supersession as supersession_module


def prepare_lineages(
    h: Harness,
    *,
    task: str = "1217410611963029",
    old_branch: str = "agent/registry-correction-import",
    replacement_branch: str = "agent/postgresql-prepare-parity",
) -> tuple[str, str, str]:
    base = h.current_remote_main()
    h.agent_file("old-agent")
    h.agent_file("new-agent", owning_task_gid=task)
    h.start(task=task, branch=old_branch, base=base, agent="old-agent")
    old_head = h.commit_local(task, "old published implementation")
    h.tool("publish", "--task", task)
    replacement_head = h.remote_branch_commit(
        replacement_branch,
        "replacement published implementation",
        start=base,
    )
    return base, old_head, replacement_head


def supersede_args(
    *,
    task: str,
    old_branch: str,
    old_head: str,
    replacement_branch: str,
    base: str,
    replacement_head: str,
    reason: str = "canonical task scope superseded the old implementation lineage",
    provenance: str = "Asana task 1217410611963029 canonical replacement handoff",
) -> list[str]:
    return [
        "supersede",
        "--task",
        task,
        "--old-branch",
        old_branch,
        "--old-head",
        old_head,
        "--branch",
        replacement_branch,
        "--base-ref",
        "refs/heads/main",
        "--base",
        base,
        "--expected-head",
        replacement_head,
        "--pr-number",
        "92",
        "--pr-head",
        replacement_head,
        "--pr-lease-state",
        "none",
        "--agent-id",
        "new-agent",
        "--reason",
        reason,
        "--provenance",
        provenance,
        "--json",
    ]


def test_exact_pr92_supersession_preserves_old_history_and_activates_replacement(h: Harness) -> None:
    task = "1217410611963029"
    old_branch = "agent/registry-correction-import"
    replacement_branch = "agent/postgresql-prepare-parity"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=replacement_branch)

    result = h.raw_tool(*supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=replacement_branch, base=base, replacement_head=replacement_head))
    payload = json.loads(result.stdout)
    state = h.state(task)

    assert payload["ok"] is True and payload["idempotent"] is False
    assert state["lifecycle"] == "active"
    assert state["branch"] == replacement_branch
    assert state["local_head"] == replacement_head
    assert state["pr_url"].endswith("/pull/92")
    assert state["pr_head"] == replacement_head
    assert git_out(h.origin, "rev-parse", f"refs/heads/{old_branch}") == old_head
    assert git_out(h.wt(task), "rev-parse", "HEAD") == replacement_head

    prior = state["prior_lineages"]
    assert len(prior) == 1
    archived = prior[0]
    assert archived["branch"] == old_branch
    assert archived["terminal_head"] == old_head
    assert archived["base_ref"] == "refs/heads/main"
    assert archived["base_sha"] == base
    assert archived["disposition"] == "superseded"
    assert archived["owner"]["agent_id"] == "old-agent"
    assert archived["replacement"]["branch"] == replacement_branch
    assert archived["replacement"]["pr"]["number"] == 92
    assert archived["reason"]
    assert archived["provenance"]
    assert state["supersession"]["phase"] == "complete"

    resumed = h.raw_tool(
        "claim",
        "--task", task,
        "--branch", replacement_branch,
        "--agent-id", "new-agent",
        "--pr-number", "92",
        "--pr-head", replacement_head,
        "--pr-lease-state", "none",
        "--",
        "python3", str(SCRIPT),
        "resume", "--task", task, "--agent-id", "new-agent", "--json",
    )
    assert json.loads(resumed.stdout)["branch"] == replacement_branch


def test_ordinary_cross_lineage_claim_still_fails_without_explicit_supersession(h: Harness) -> None:
    task = "4101"
    old_branch = "agent/old-lineage"
    base, old_head, replacement_head = prepare_lineages(
        h, task=task, old_branch=old_branch, replacement_branch="agent/new-lineage"
    )
    del base, old_head, replacement_head
    claim_files = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}*.json"))
    prior = json.loads(claim_files[0].read_text(encoding="utf-8"))
    result = h.raw_tool(
        "claim",
        "--task",
        task,
        "--branch",
        "agent/new-lineage",
        "--agent-id",
        "new-agent",
        "--takeover",
        "--expected-claim",
        str(prior["token"]),
        "--",
        "python3",
        "-c",
        "pass",
        check=False,
    )
    assert_error(result, "OWNERSHIP_AMBIGUOUS")


def test_dirty_old_worktree_refuses_without_state_change(h: Harness) -> None:
    task, old_branch, new_branch = "4102", "agent/dirty-old", "agent/dirty-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    (h.wt(task) / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    result = h.raw_tool(*supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head), check=False)
    assert_error(result, "DIRTY_SUPERSESSION")
    state = h.state(task)
    assert state["lifecycle"] == "active" and state["branch"] == old_branch
    assert "prior_lineages" not in state


def test_old_head_movement_refuses_without_terminalization(h: Harness) -> None:
    task, old_branch, new_branch = "4103", "agent/moved-old", "agent/moved-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    h.remote_branch_commit(old_branch, "old branch moved")
    result = h.raw_tool(*supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head), check=False)
    assert_error(result, "EXPECTED_HEAD_MISMATCH")
    assert h.state(task)["lifecycle"] == "active"


def test_replacement_head_movement_refuses_without_terminalization(h: Harness) -> None:
    task, old_branch, new_branch = "4104", "agent/stable-old", "agent/moved-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    h.remote_branch_commit(new_branch, "replacement moved")
    result = h.raw_tool(*supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head), check=False)
    assert_error(result, "EXPECTED_HEAD_MISMATCH")
    assert h.state(task)["lifecycle"] == "active"


def test_live_claim_conflict_refuses_supersession(h: Harness) -> None:
    task, old_branch, new_branch = "4105", "agent/live-old", "agent/live-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    process = subprocess.Popen(
        [
            "python3",
            str(SCRIPT),
            "claim",
            "--task",
            task,
            "--branch",
            old_branch,
            "--agent-id",
            "old-agent",
            "--",
            "python3",
            "-c",
            "import time; time.sleep(2)",
        ],
        cwd=h.primary,
        env=h.env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    claim_file = None
    while time.monotonic() < deadline:
        matches = list((h.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}*.json"))
        if matches:
            current = json.loads(matches[0].read_text(encoding="utf-8"))
            if current.get("released_at") is None:
                claim_file = matches[0]
                break
        time.sleep(0.02)
    assert claim_file is not None
    result = h.raw_tool(*supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head), check=False)
    assert_error(result, "OWNERSHIP_CLAIMED")
    assert h.state(task)["lifecycle"] == "active"
    process.communicate(timeout=10)


def test_unrecoverable_old_head_refuses(h: Harness) -> None:
    task, old_branch, new_branch = "4106", "agent/unrecoverable-old", "agent/unrecoverable-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    git(h.origin, "update-ref", "-d", f"refs/heads/{old_branch}", old_head)
    result = h.raw_tool(*supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head), check=False)
    assert_error(result, "ONLY_RECOVERY_COPY")
    assert h.state(task)["lifecycle"] == "active"


def _namespace(args: list[str], *, repo: str) -> argparse.Namespace:
    values: dict[str, object] = {"repo": repo, "pr_lease_id": None}
    it = iter(args[1:])
    for token in it:
        if token == "--json":
            values["json"] = True
            continue
        value = next(it)
        key = token[2:].replace("-", "_")
        values[key] = int(value) if key == "pr_number" else value
    return argparse.Namespace(**values)


def test_failure_before_terminal_provenance_commit_leaves_old_lineage_active(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    task, old_branch, new_branch = "4110", "agent/precommit-old", "agent/precommit-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    args_list = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    disk_before = h.state(task)

    for key in ("HOME", "DISH_WORKTREE_ROOT", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "TEST_BARE_ORIGIN"):
        monkeypatch.setenv(key, h.env[key])
    for key in list(os.environ):
        if key.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(key, raising=False)

    def fail_first_progress(*args: object, **kwargs: object) -> None:
        raise AgentWorktreeError("TEST_BEFORE_TERMINALIZATION", "injected pre-persistence failure")

    monkeypatch.setattr(supersession_module, "_write_progress", fail_first_progress)
    runner = GitRunner()
    with pytest.raises(AgentWorktreeError, match="pre-persistence failure"):
        supersession_module.command_supersede(_namespace(args_list, repo=str(h.primary)), runner)
    runner.close()

    assert h.state(task) == disk_before
    assert h.wt(task).exists()
    assert git_out(h.wt(task), "rev-parse", "HEAD") == old_head
    assert git_out(h.origin, "rev-parse", f"refs/heads/{old_branch}") == old_head


def test_failure_after_terminalization_is_explicit_and_exact_retry_recovers(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    task, old_branch, new_branch = "4107", "agent/crash-old", "agent/crash-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    args_list = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)

    for key in ("HOME", "DISH_WORKTREE_ROOT", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "TEST_BARE_ORIGIN"):
        monkeypatch.setenv(key, h.env[key])
    for key in list(os.environ):
        if key.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(key, raising=False)

    def injected(*args: object, **kwargs: object):
        raise AgentWorktreeError("TEST_AFTER_TERMINALIZATION", "injected failure")

    monkeypatch.setattr(supersession_module, "_adopt_remote_branch_locked", injected)
    runner = GitRunner()
    with pytest.raises(AgentWorktreeError, match="injected failure"):
        supersession_module.command_supersede(_namespace(args_list, repo=str(h.primary)), runner)
    runner.close()

    state = h.state(task)
    assert state["lifecycle"] == "supersession-incomplete"
    assert state["supersession"]["phase"] == "terminalized"
    assert state["prior_lineages"][0]["terminal_head"] == old_head
    assert git_out(h.origin, "rev-parse", f"refs/heads/{old_branch}") == old_head
    status = json.loads(h.raw_tool("status", "--task", task, "--json").stdout)
    assert "supersession incomplete" in status["diagnostics"][0]

    monkeypatch.undo()
    result = h.raw_tool(*args_list)
    assert json.loads(result.stdout)["idempotent"] is False
    assert h.state(task)["lifecycle"] == "active"
    assert h.state(task)["branch"] == new_branch


def _configure_direct_supersession_env(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("HOME", "DISH_WORKTREE_ROOT", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "TEST_BARE_ORIGIN"):
        monkeypatch.setenv(key, h.env[key])
    for key in list(os.environ):
        if key.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(key, raising=False)


def test_exact_retry_recovers_when_old_worktree_was_already_unlocked(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    task, old_branch, new_branch = "4111", "agent/unlocked-old", "agent/unlocked-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    args_list = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    _configure_direct_supersession_env(h, monkeypatch)

    runner = GitRunner()
    real_run = runner.run
    crashed = False

    def crash_before_remove(cwd, *args, **kwargs):
        nonlocal crashed
        if not crashed and args[:2] == ("worktree", "remove"):
            crashed = True
            raise SystemExit("injected process death after worktree unlock")
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(runner, "run", crash_before_remove)
    with pytest.raises(SystemExit, match="after worktree unlock"):
        supersession_module.command_supersede(_namespace(args_list, repo=str(h.primary)), runner)
    runner.close()

    state = h.state(task)
    assert state["lifecycle"] == "supersession-incomplete"
    assert h.wt(task).exists()
    listing = git_out(h.primary, "worktree", "list", "--porcelain")
    block = next(part for part in listing.split("\n\n") if str(h.wt(task)) in part)
    assert not any(line.startswith("locked ") for line in block.splitlines())

    monkeypatch.undo()
    result = json.loads(h.raw_tool(*args_list).stdout)
    assert result["ok"] is True and result["idempotent"] is False
    assert h.state(task)["branch"] == new_branch
    assert git_out(h.origin, "rev-parse", f"refs/heads/{old_branch}") == old_head


def test_unlocked_old_worktree_with_moved_head_still_fails_closed(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    task, old_branch, new_branch = "4112", "agent/unlocked-moved-old", "agent/unlocked-moved-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    args_list = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    _configure_direct_supersession_env(h, monkeypatch)

    runner = GitRunner()
    real_run = runner.run
    crashed = False

    def crash_before_remove(cwd, *args, **kwargs):
        nonlocal crashed
        if not crashed and args[:2] == ("worktree", "remove"):
            crashed = True
            raise SystemExit("injected process death after worktree unlock")
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(runner, "run", crash_before_remove)
    with pytest.raises(SystemExit):
        supersession_module.command_supersede(_namespace(args_list, repo=str(h.primary)), runner)
    runner.close()
    monkeypatch.undo()

    (h.wt(task) / "moved.txt").write_text("moved\n", encoding="utf-8")
    git(h.wt(task), "add", "moved.txt")
    git(h.wt(task), "commit", "-m", "move old local head")
    result = h.raw_tool(*args_list, check=False)
    assert_error(result, "EXPECTED_HEAD_MISMATCH")
    assert h.state(task)["lifecycle"] == "supersession-incomplete"
    assert git_out(h.origin, "rev-parse", f"refs/heads/{old_branch}") == old_head


def test_exact_retry_recovers_branch_only_partial_replacement_adoption(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    task, old_branch, new_branch = "4113", "agent/ref-only-old", "agent/ref-only-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    args_list = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    _configure_direct_supersession_env(h, monkeypatch)

    runner = GitRunner()
    real_run = runner.run
    crashed = False

    def crash_before_worktree_add(cwd, *args, **kwargs):
        nonlocal crashed
        if not crashed and args[:2] == ("worktree", "add"):
            crashed = True
            raise SystemExit("injected process death after replacement ref creation")
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(runner, "run", crash_before_worktree_add)
    with pytest.raises(SystemExit, match="after replacement ref creation"):
        supersession_module.command_supersede(_namespace(args_list, repo=str(h.primary)), runner)
    runner.close()

    state = h.state(task)
    assert state["lifecycle"] == "supersession-incomplete"
    assert state["supersession"]["replacement_activation_started"] is True
    assert git_out(h.primary, "rev-parse", f"refs/heads/{new_branch}") == replacement_head
    assert not h.wt(task).exists()

    monkeypatch.undo()
    result = json.loads(h.raw_tool(*args_list).stdout)
    assert result["ok"] is True and result["idempotent"] is False
    assert h.state(task)["branch"] == new_branch
    assert git_out(h.wt(task), "rev-parse", "HEAD") == replacement_head


def test_branch_only_partial_with_moved_ref_still_fails_closed(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    task, old_branch, new_branch = "4114", "agent/ref-moved-old", "agent/ref-moved-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    args_list = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    _configure_direct_supersession_env(h, monkeypatch)

    runner = GitRunner()
    real_run = runner.run
    crashed = False

    def crash_before_worktree_add(cwd, *args, **kwargs):
        nonlocal crashed
        if not crashed and args[:2] == ("worktree", "add"):
            crashed = True
            raise SystemExit("injected process death after replacement ref creation")
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(runner, "run", crash_before_worktree_add)
    with pytest.raises(SystemExit):
        supersession_module.command_supersede(_namespace(args_list, repo=str(h.primary)), runner)
    runner.close()
    monkeypatch.undo()

    git(h.primary, "update-ref", f"refs/heads/{new_branch}", base, replacement_head)
    result = h.raw_tool(*args_list, check=False)
    assert_error(result, "EXPECTED_HEAD_MISMATCH")
    assert h.state(task)["lifecycle"] == "supersession-incomplete"
    assert git_out(h.primary, "rev-parse", f"refs/heads/{new_branch}") == base


def test_successful_exact_retry_is_idempotent_and_changed_identity_fails(h: Harness) -> None:
    task, old_branch, new_branch = "4108", "agent/retry-old", "agent/retry-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)
    args_list = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    first = json.loads(h.raw_tool(*args_list).stdout)
    second = json.loads(h.raw_tool(*args_list).stdout)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    state_before = h.state(task)

    changed = supersede_args(
        task=task,
        old_branch=old_branch,
        old_head=old_head,
        replacement_branch=new_branch,
        base=base,
        replacement_head=base,
    )
    result = h.raw_tool(*changed, check=False)
    assert_error(result, "SUPERSESSION_IDENTITY_MISMATCH")
    assert h.state(task) == state_before


def test_missing_provenance_live_pr_lease_and_protected_repository_guards_refuse(h: Harness) -> None:
    task, old_branch, new_branch = "4109", "agent/guard-old", "agent/guard-new"
    base, old_head, replacement_head = prepare_lineages(h, task=task, old_branch=old_branch, replacement_branch=new_branch)

    no_provenance = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    index = no_provenance.index("--provenance") + 1
    no_provenance[index] = ""
    assert_error(h.raw_tool(*no_provenance, check=False), "SUPERSESSION_PROVENANCE_REQUIRED")

    live_lease = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head)
    lease_index = live_lease.index("--pr-lease-state") + 1
    live_lease[lease_index] = "active"
    live_lease[live_lease.index("--agent-id"):live_lease.index("--agent-id")] = ["--pr-lease-id", "lease-123"]
    assert_error(h.raw_tool(*live_lease, check=False), "SUPERSESSION_LIVE_PR_LEASE")

    protected = supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch="main", base=base, replacement_head=replacement_head)
    assert_error(h.raw_tool(*protected, check=False), "INVALID_BRANCH")

    hostile_env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.sshCommand", "GIT_CONFIG_VALUE_0": "false"}
    assert_error(h.raw_tool(*supersede_args(task=task, old_branch=old_branch, old_head=old_head, replacement_branch=new_branch, base=base, replacement_head=replacement_head), check=False, env=hostile_env), "GIT_ENV_OVERRIDE")
