from __future__ import annotations

import argparse
import socket
from pathlib import Path
from typing import Any

from .common import (AgentWorktreeError, EXPECTED_REPOSITORY, SCHEMA_VERSION, GitRunner, fail, now_utc, require_agent_id, require_full_sha, require_task_gid, worktree_records)
from .operations import (
    branch_exists, candidate_path_is_safe, payload_from_state, remote_and_target_observation,
    owner_agent_id, remote_ref_sha, resolve_repository_from_state, update_owner, validate_base_ref,
    validate_branch, verify_owned_worktree, ensure_commit_object, record_observation,
)
from .repository import discover_repository
from .state import (
    TaskLock, atomic_write_json, clear_agent_reference, load_task_state, set_agent_reference,
    state_path, task_worktree_path, validate_agent_state,
)

def command_start(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    agent_id = require_agent_id(args.agent_id)
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

        # Build provisional state from actual Git identity; no state file is written
        # until every top-level/git-dir/common-dir/registry/branch/HEAD check passes.
        git_dir = runner.path(candidate, "--git-dir")
        provisional = {
            "schema_version": SCHEMA_VERSION,
            "repository": {"full_name": EXPECTED_REPOSITORY, "origin_id": repo.origin_id},
            "task_gid": task_gid,
            "branch": branch,
            "worktree_path": str(candidate),
            "git_common_dir": str(repo.common_dir),
            "git_dir": str(git_dir),
            "base_ref": base_ref,
            "base_sha": base_sha,
            "owner": {"agent_id": agent_id, "host": socket.gethostname()},
            "created_at": now_utc(),
            "last_verified_at": now_utc(),
            "local_head": base_sha,
            "published_head": None,
            "remote_owned_head": None,
            "remote_relation": "absent",
            "remote_checked_at": now_utc(),
            "target_current_head": remote_base,
            "target_checked_at": now_utc(),
            "pr_url": None,
            "pr_head": None,
            "lifecycle": "active",
            "disposition": None,
        }
        identity = verify_owned_worktree(runner, repo, provisional)
        if identity.head != base_sha:
            fail("WORKTREE_CREATE_VERIFY_FAILED", f"created worktree HEAD {identity.head} != exact base {base_sha}")
        provisional["last_verified_at"] = now_utc()
        provisional["local_head"] = identity.head
        atomic_write_json(state_file, provisional)
        if agent_id is not None:
            set_agent_reference(agent_id, provisional)
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
        clear_agent_reference(previous_owner, task_gid)
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
    return payload_from_state(command_name, state, identity, relation=relation, remote_head=remote_head, target_head=target_head)


def command_resume(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    agent_id = require_agent_id(args.agent_id)
    if args.takeover and agent_id is None:
        fail("TAKEOVER_REQUIRES_AGENT", "--takeover requires --agent-id")
    with TaskLock(task_gid):
        return resume_locked(task_gid, agent_id, args.takeover, runner)


def command_status(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    state = load_task_state(task_gid)
    if state.get("lifecycle") != "active":
        return {
            "command": "status",
            "ok": True,
            "task_gid": task_gid,
            "lifecycle": state.get("lifecycle"),
            "disposition": state.get("disposition"),
            "branch": state.get("branch"),
            "worktree": state.get("worktree_path"),
            "base_ref": state.get("base_ref"),
            "base_sha": state.get("base_sha"),
            "local_head": state.get("local_head"),
            "published_head": state.get("published_head"),
            "owner_agent_id": owner_agent_id(state),
            "state_path": str(state_path(task_gid)),
            "diagnostics": ["task is no longer active; live worktree verification was not attempted"],
        }
    try:
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
    except AgentWorktreeError as exc:
        return {
            "command": "status",
            "ok": False,
            "task_gid": task_gid,
            "branch": state.get("branch"),
            "worktree": state.get("worktree_path"),
            "base_ref": state.get("base_ref"),
            "base_sha": state.get("base_sha"),
            "local_head": state.get("local_head"),
            "published_head": state.get("published_head"),
            "owner_agent_id": owner_agent_id(state),
            "state_path": str(state_path(task_gid)),
            "diagnostics": [f"{exc.code}: {exc.message}"],
        }
    payload = payload_from_state("status", state, identity)
    payload["diagnostics"] = []
    return payload
