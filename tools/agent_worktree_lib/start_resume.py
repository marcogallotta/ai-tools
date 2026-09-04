from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import (AgentWorktreeError, GitRunner, fail, now_utc, resolve_agent_id, require_full_sha, require_task_gid, worktree_records, find_worktree_record)
from .operations import (
    branch_exists, candidate_path_is_safe, payload_from_state, remote_and_target_observation,
    owner_agent_id, remote_ref_sha, resolve_repository_from_state, update_owner, validate_base_ref,
    validate_branch, verify_owned_worktree, ensure_commit_object, record_observation,
)
from .repository import discover_repository
from .tool_environment import preflight_tool_environment
from .state import (
    TaskLock, atomic_write_json, clear_agent_reference, load_task_state, new_active_task_state,
    read_json_object, set_agent_reference, state_path, task_state_paths, task_worktree_path, validate_agent_state,
)

def _checked_out_path(runner: GitRunner, cwd: Path, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    for record in worktree_records(runner, cwd):
        if record.get("branch") == ref:
            return record.get("worktree") or "<unknown>"
    return None


def _rollback_local_adoption(
    runner: GitRunner,
    *,
    repo,
    candidate: Path,
    branch: str,
    expected_head: str,
    owns_branch: bool,
) -> str | None:
    """Best-effort removal of only the worktree/ref this adopt attempt provably created."""
    errors: list[str] = []
    ref = f"refs/heads/{branch}"
    record = find_worktree_record(worktree_records(runner, repo.source_top), candidate)
    if record is not None:
        if record.get("branch") != ref:
            errors.append(f"candidate worktree is registered to unexpected branch {record.get('branch')!r}")
        else:
            unlock = runner.run(repo.source_top, "worktree", "unlock", str(candidate), check=False)
            if unlock.returncode != 0:
                errors.append(f"worktree unlock failed: {unlock.stderr.strip()}")
            else:
                remove = runner.run(repo.source_top, "worktree", "remove", str(candidate), check=False)
                if remove.returncode != 0:
                    errors.append(f"worktree remove failed: {remove.stderr.strip()}")

    if owns_branch and branch_exists(runner, repo.source_top, branch):
        checked = _checked_out_path(runner, repo.source_top, branch)
        if checked is not None:
            errors.append(f"local branch remained checked out at {checked}")
        else:
            actual = runner.sha(repo.source_top, ref)
            if actual != expected_head:
                errors.append(f"local branch moved during rollback: {actual} != {expected_head}")
            else:
                deleted = runner.run(repo.source_top, "update-ref", "-d", ref, expected_head, check=False)
                if deleted.returncode != 0:
                    errors.append(f"conditional local branch deletion failed: {deleted.stderr.strip()}")
    return "; ".join(errors) if errors else None


def _validate_adoption_remote(
    runner: GitRunner,
    *,
    repo,
    branch: str,
    base_ref: str,
    base_sha: str,
    expected_head: str,
) -> str:
    remote_ref = f"refs/heads/{branch}"
    remote_head = remote_ref_sha(runner, repo, remote_ref, allow_missing=True)
    if remote_head is None:
        fail("REMOTE_BRANCH_MISSING", f"explicit handoff branch does not exist on verified origin: {branch}")
    if remote_head != expected_head:
        fail(
            "EXPECTED_HEAD_MISMATCH",
            f"remote handoff branch moved or was misidentified: expected {expected_head}, origin has {remote_head}; no state/worktree was created",
        )

    target_head = remote_ref_sha(runner, repo, base_ref)
    assert target_head is not None
    ensure_commit_object(runner, repo, base_sha)
    ensure_commit_object(runner, repo, expected_head)
    if runner.sha(repo.source_top, base_sha) != base_sha:
        fail("BASE_FETCH_FAILED", "supplied base does not resolve to the exact fetched commit")
    if runner.sha(repo.source_top, expected_head) != expected_head:
        fail("HANDOFF_FETCH_FAILED", "expected remote branch HEAD does not resolve to the exact fetched commit")
    ancestor = runner.run(repo.source_top, "merge-base", "--is-ancestor", base_sha, expected_head, check=False)
    if ancestor.returncode == 1:
        fail(
            "BASE_NOT_ANCESTOR",
            f"supplied authoring base {base_sha} is not an ancestor of handed-off head {expected_head}",
        )
    if ancestor.returncode != 0:
        fail("ANCESTRY_CHECK_FAILED", "could not verify the supplied base against the handed-off branch head")
    return target_head


def _adopt_remote_branch_locked(
    runner: GitRunner,
    *,
    task_gid: str,
    agent_id: str | None,
    repo,
    branch: str,
    base_ref: str,
    base_sha: str,
    expected_head: str,
    allow_exact_local_retry: bool = False,
) -> tuple[dict[str, Any], Any, str]:
    candidate = task_worktree_path(task_gid).resolve()
    runner.check_candidate(candidate)

    target_head = _validate_adoption_remote(
        runner,
        repo=repo,
        branch=branch,
        base_ref=base_ref,
        base_sha=base_sha,
        expected_head=expected_head,
    )

    remote_ref = f"refs/heads/{branch}"
    local_branch_exists = branch_exists(runner, repo.source_top, branch)
    registered = find_worktree_record(worktree_records(runner, repo.source_top), candidate)
    exact_retry = False
    exact_ref_only_retry = False
    if allow_exact_local_retry and local_branch_exists:
        checked = _checked_out_path(runner, repo.source_top, branch)
        actual = runner.sha(repo.source_top, remote_ref)
        if checked is not None and Path(checked).resolve() == candidate and registered is not None:
            if actual == expected_head and registered.get("branch") == remote_ref and registered.get("HEAD") == expected_head:
                exact_retry = True
        elif checked is None and registered is None:
            if actual != expected_head:
                fail(
                    "EXPECTED_HEAD_MISMATCH",
                    f"partial retry local branch moved: expected {expected_head}, local has {actual}",
                )
            candidate_path_is_safe(repo, runner, candidate)
            # The caller has durably authorized exact local retry before entering this
            # helper. That external journal is what makes this otherwise-forbidden
            # branch-only state attributable to a ref creation that survived process
            # death before `git worktree add` registered the replacement worktree.
            exact_ref_only_retry = True
    if not exact_retry and not exact_ref_only_retry:
        candidate_path_is_safe(repo, runner, candidate)
        if local_branch_exists:
            checked = _checked_out_path(runner, repo.source_top, branch)
            if checked is not None:
                fail("BRANCH_CHECKED_OUT", f"handoff branch already exists and is checked out elsewhere: {checked}")
            fail("BRANCH_COLLISION", f"handoff branch already exists locally without matching task state: {branch}")

    verified_remote = remote_ref_sha(runner, repo, remote_ref)
    assert verified_remote is not None
    if verified_remote != expected_head:
        fail(
            "EXPECTED_HEAD_MISMATCH",
            f"remote handoff branch moved before adoption: expected {expected_head}, origin has {verified_remote}; no state/worktree was created",
        )

    owns_branch = exact_ref_only_retry
    if not exact_retry:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if not exact_ref_only_retry:
            create_ref = runner.run(
                repo.source_top, "update-ref", remote_ref, expected_head, "0" * 40, check=False,
            )
            if create_ref.returncode != 0:
                fail(
                    "BRANCH_CREATE_RACE",
                    f"local branch ref creation for handoff failed, likely raced by another local creator: {create_ref.stderr.strip()}",
                )
            owns_branch = True
        reason = f"Dish task {task_gid}; adopted remote branch; agent {agent_id or 'unrecorded'}"
        add = runner.run(
            repo.source_top,
            "worktree",
            "add",
            "--lock",
            "--reason",
            reason,
            str(candidate),
            branch,
            check=False,
        )
        if add.returncode != 0:
            rollback_error = _rollback_local_adoption(
                runner,
                repo=repo,
                candidate=candidate,
                branch=branch,
                expected_head=expected_head,
                owns_branch=owns_branch,
            )
            if rollback_error:
                fail(
                    "ADOPTION_ROLLBACK_FAILED",
                    f"git worktree adoption failed and local rollback was incomplete: {rollback_error}",
                )
            fail("WORKTREE_ADOPT_FAILED", f"git worktree add failed without durable state: {add.stderr.strip()}")

    try:
        git_dir = runner.path(candidate, "--git-dir")
        provisional = new_active_task_state(
            task_gid=task_gid,
            branch=branch,
            worktree_path=candidate,
            git_common_dir=repo.common_dir,
            git_dir=git_dir,
            origin_id=repo.origin_id,
            base_ref=base_ref,
            base_sha=base_sha,
            agent_id=agent_id,
            local_head=expected_head,
            published_head=expected_head,
            remote_owned_head=expected_head,
            remote_relation="equal",
            target_current_head=target_head,
        )
        identity = verify_owned_worktree(runner, repo, provisional)
        if identity.head != expected_head:
            fail(
                "WORKTREE_ADOPT_VERIFY_FAILED",
                f"adopted worktree HEAD {identity.head} != exact handed-off head {expected_head}",
            )
        if identity.dirty:
            fail("WORKTREE_ADOPT_VERIFY_FAILED", "adopted worktree is unexpectedly dirty")

        verified_remote = remote_ref_sha(runner, repo, remote_ref)
        assert verified_remote is not None
        if verified_remote != expected_head:
            fail(
                "EXPECTED_HEAD_MOVED",
                f"remote handoff branch moved during adoption: expected {expected_head}, origin has {verified_remote}",
            )
        current_target = remote_ref_sha(runner, repo, base_ref)
        assert current_target is not None
        provisional["target_current_head"] = current_target
    except AgentWorktreeError as exc:
        if not exact_retry:
            rollback_error = _rollback_local_adoption(
                runner,
                repo=repo,
                candidate=candidate,
                branch=branch,
                expected_head=expected_head,
                owns_branch=owns_branch,
            )
            if rollback_error:
                fail(
                    "ADOPTION_ROLLBACK_FAILED",
                    f"post-create adoption verification failed with {exc.code} and rollback was incomplete: {rollback_error}",
                )
        raise
    return provisional, identity, current_target


def command_adopt(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    agent_id = resolve_agent_id(args.agent_id)
    base_sha = require_full_sha(args.base, "supplied base SHA")
    expected_head = require_full_sha(args.expected_head, "expected remote branch HEAD")
    validate_agent_state(agent_id)

    with TaskLock(task_gid):
        state_file = state_path(task_gid)
        if state_file.exists():
            fail(
                "ADOPTION_STATE_EXISTS",
                "task already has durable worktree state; use resume/--takeover instead of adopting the remote branch again",
            )
        repo = discover_repository(runner, Path(args.repo))
        branch = validate_branch(runner, repo.source_top, args.branch)
        base_ref = validate_base_ref(runner, repo.source_top, args.base_ref)
        provisional, identity, current_target = _adopt_remote_branch_locked(
            runner,
            task_gid=task_gid,
            agent_id=agent_id,
            repo=repo,
            branch=branch,
            base_ref=base_ref,
            base_sha=base_sha,
            expected_head=expected_head,
        )
        atomic_write_json(state_file, provisional)
        if agent_id is not None:
            set_agent_reference(agent_id, provisional)
        preflight_tool_environment(
            task_gid=task_gid, agent_id=agent_id, worktree=identity.path, head=identity.head
        )
        return payload_from_state(
            "adopt",
            provisional,
            identity,
            relation="equal",
            remote_head=expected_head,
            target_head=current_target,
        )


def command_start(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    agent_id = resolve_agent_id(args.agent_id)
    base_sha = require_full_sha(args.base, "supplied base SHA")
    validate_agent_state(agent_id)
    with TaskLock(task_gid):
        state_file = state_path(task_gid)
        if state_file.exists():
            state = load_task_state(task_gid)
            if state["branch"] != args.branch or state["base_ref"] != args.base_ref or state["base_sha"] != base_sha:
                fail(
                    "STATE_CONTRADICTION",
                    "task already has durable worktree state with a different branch/base; use resume or explicit recovery",
                )
            return resume_locked(task_gid, agent_id, False, runner, command_name="start")

        repo = discover_repository(runner, Path(args.repo))
        branch = validate_branch(runner, repo.source_top, args.branch)
        base_ref = validate_base_ref(runner, repo.source_top, args.base_ref)
        candidate = task_worktree_path(task_gid).resolve()
        runner.check_candidate(candidate)
        candidate_path_is_safe(repo, runner, candidate)

        if branch_exists(runner, repo.source_top, branch):
            checked = [r.get("worktree") for r in worktree_records(runner, repo.source_top) if r.get("branch") == f"refs/heads/{branch}"]
            if checked:
                fail("BRANCH_CHECKED_OUT", f"owned branch already exists and is checked out elsewhere: {checked[0]}")
            fail("BRANCH_COLLISION", f"owned branch already exists without matching task state: {branch}")
        remote_owned = remote_ref_sha(runner, repo, f"refs/heads/{branch}", allow_missing=True)
        if remote_owned is not None:
            fail("REMOTE_BRANCH_COLLISION", f"remote owned branch already exists without matching task state: {branch} at {remote_owned}")

        remote_base = remote_ref_sha(runner, repo, base_ref)
        assert remote_base is not None
        if remote_base != base_sha:
            fail(
                "STALE_HANDOFF_BASE",
                f"supplied base {base_sha} does not equal current origin {base_ref} {remote_base}; no branch/worktree was created",
            )
        ensure_commit_object(runner, repo, base_sha)
        if runner.sha(repo.source_top, base_sha) != base_sha:
            fail("BASE_FETCH_FAILED", "supplied base does not resolve to the exact fetched commit")

        candidate.parent.mkdir(parents=True, exist_ok=True)
        reason = f"Dish task {task_gid}; agent {agent_id or 'unrecorded'}"
        add = runner.run(
            repo.source_top,
            "worktree",
            "add",
            "--lock",
            "--reason",
            reason,
            "-b",
            branch,
            str(candidate),
            base_sha,
            check=False,
        )
        if add.returncode != 0:
            fail("WORKTREE_CREATE_FAILED", f"git worktree add failed without recovery mutation: {add.stderr.strip()}")

        git_dir = runner.path(candidate, "--git-dir")
        provisional = new_active_task_state(
            task_gid=task_gid,
            branch=branch,
            worktree_path=candidate,
            git_common_dir=repo.common_dir,
            git_dir=git_dir,
            origin_id=repo.origin_id,
            base_ref=base_ref,
            base_sha=base_sha,
            agent_id=agent_id,
            local_head=base_sha,
            published_head=None,
            remote_owned_head=None,
            remote_relation="absent",
            target_current_head=remote_base,
        )
        identity = verify_owned_worktree(runner, repo, provisional)
        if identity.head != base_sha:
            fail("WORKTREE_CREATE_VERIFY_FAILED", f"created worktree HEAD {identity.head} != exact base {base_sha}")
        provisional["last_verified_at"] = now_utc()
        provisional["local_head"] = identity.head
        atomic_write_json(state_file, provisional)
        if agent_id is not None:
            set_agent_reference(agent_id, provisional)
        preflight_tool_environment(
            task_gid=task_gid, agent_id=agent_id, worktree=identity.path, head=identity.head
        )
        return payload_from_state("start", provisional, identity, relation="absent", remote_head=None, target_head=remote_base)


def resume_locked(
    task_gid: str,
    agent_id: str | None,
    takeover: bool,
    runner: GitRunner,
    *,
    command_name: str = "resume",
) -> dict[str, Any]:
    state = load_task_state(task_gid)
    validate_agent_state(agent_id)
    previous_owner = owner_agent_id(state)
    update_owner(state, agent_id, takeover)
    repo = resolve_repository_from_state(runner, state)
    identity = verify_owned_worktree(runner, repo, state)
    relation, remote_head, target_head, _ = remote_and_target_observation(runner, repo, state, identity.head)
    record_observation(state, identity, relation, remote_head, target_head)
    atomic_write_json(state_path(task_gid), state)
    if agent_id is not None:
        set_agent_reference(agent_id, state)
    if takeover and previous_owner is not None and previous_owner != agent_id:
        clear_agent_reference(previous_owner, task_gid, state.get("lineage_id"))
    if relation == "remote-ahead":
        fail(
            "REMOTE_AHEAD",
            f"remote owned branch {state['branch']} is ahead of local HEAD {identity.head}; explicit ownership/recovery decision required",
        )
    if relation == "divergent":
        fail(
            "REMOTE_DIVERGED",
            f"remote owned branch {state['branch']} diverged from local HEAD {identity.head}; automatic merge/rebase/force-push is refused",
        )
    preflight_tool_environment(
        task_gid=task_gid, agent_id=agent_id, worktree=identity.path, head=identity.head
    )
    return payload_from_state(command_name, state, identity, relation=relation, remote_head=remote_head, target_head=target_head)


def command_resume(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    agent_id = resolve_agent_id(args.agent_id)
    if args.takeover and agent_id is None:
        fail("TAKEOVER_REQUIRES_AGENT", "--takeover requires --agent-id")
    with TaskLock(task_gid):
        return resume_locked(task_gid, agent_id, args.takeover, runner)


def _status_one(task_gid: str, state: dict[str, Any], runner: GitRunner, path: Path) -> dict[str, Any]:
    if state.get("lifecycle") != "active":
        if state.get("lifecycle") == "supersession-incomplete":
            diagnostic = "supersession incomplete; the old lineage was already terminalized and requires an exact retry to complete"
        else:
            diagnostic = "task lineage is no longer active; live worktree verification was not attempted"
        return {
            "command": "status", "ok": True, "task_gid": task_gid,
            "lineage_id": state.get("lineage_id"), "lifecycle": state.get("lifecycle"),
            "disposition": state.get("disposition"), "branch": state.get("branch"),
            "worktree": state.get("worktree_path"), "base_ref": state.get("base_ref"),
            "base_sha": state.get("base_sha"), "local_head": state.get("local_head"),
            "published_head": state.get("published_head"), "owner_agent_id": owner_agent_id(state),
            "state_path": str(path), "supersession": state.get("supersession"),
            "diagnostics": [diagnostic],
        }
    try:
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
    except AgentWorktreeError as exc:
        return {
            "command": "status", "ok": False, "task_gid": task_gid, "lineage_id": state.get("lineage_id"),
            "branch": state.get("branch"), "worktree": state.get("worktree_path"),
            "base_ref": state.get("base_ref"), "base_sha": state.get("base_sha"),
            "local_head": state.get("local_head"), "published_head": state.get("published_head"),
            "owner_agent_id": owner_agent_id(state), "state_path": str(path),
            "diagnostics": [f"{exc.code}: {exc.message}"],
        }
    payload = payload_from_state("status", state, identity)
    payload["lineage_id"] = state.get("lineage_id")
    payload["state_path"] = str(path)
    payload["diagnostics"] = []
    return payload


def command_status(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    paths = task_state_paths(task_gid)
    if not paths:
        fail("STATE_MISSING", f"task worktree state does not exist for {task_gid}")
    entries = [_status_one(task_gid, read_json_object(path, "task worktree state"), runner, path) for path in paths]
    if len(entries) == 1:
        return entries[0]
    return {
        "command": "status", "ok": all(bool(item.get("ok")) for item in entries),
        "task_gid": task_gid, "lineage_count": len(entries), "lineages": entries,
        "diagnostics": [],
    }
