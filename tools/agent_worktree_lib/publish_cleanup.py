from __future__ import annotations

import argparse
import os
from typing import Any, NoReturn

from .common import DISPOSITIONS, GitRunner, fail, now_utc, require_task_gid
from .operations import (
    payload_from_state, record_observation, remote_and_target_observation, remote_ref_sha,
    remote_relation, resolve_repository_from_state, verify_owned_worktree, ensure_commit_object,
)
from .state import TaskLock, atomic_write_json, clear_agent_reference, load_task_state, state_path
from .operations import owner_agent_id

def command_publish(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    with TaskLock(task_gid):
        state = load_task_state(task_gid)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        relation, remote_head = remote_relation(runner, repo, state["branch"], identity.head)
        if relation == "remote-ahead":
            fail("REMOTE_AHEAD", "remote owned branch is ahead; publish refuses automatic synchronization")
        if relation == "divergent":
            fail("REMOTE_DIVERGED", "remote owned branch diverged; publish refuses merge/rebase/force-push")
        if relation != "equal":
            ref = f"refs/heads/{state['branch']}"
            refspec = f"{ref}:{ref}"
            result = runner.run(repo.source_top, "push", repo.origin_url, refspec, check=False)
            if result.returncode != 0:
                fail("PUBLISH_FAILED", f"explicit owned-branch push failed without force: {result.stderr.strip()}")
        verified_remote = remote_ref_sha(runner, repo, f"refs/heads/{state['branch']}")
        assert verified_remote is not None
        if verified_remote != identity.head:
            fail("PUBLISH_VERIFY_FAILED", f"remote owned head {verified_remote} != local HEAD {identity.head}")
        state["published_head"] = identity.head
        state["remote_owned_head"] = verified_remote
        state["remote_relation"] = "equal"
        state["remote_checked_at"] = now_utc()
        state["last_verified_at"] = now_utc()
        state["local_head"] = identity.head
        atomic_write_json(state_path(task_gid), state)
        return payload_from_state("publish", state, identity, relation="equal", remote_head=verified_remote)


def command_verify_handoff(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    with TaskLock(task_gid):
        state = load_task_state(task_gid)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        relation, remote_head, target_head, moved = remote_and_target_observation(runner, repo, state, identity.head)
        record_observation(state, identity, relation, remote_head, target_head)
        atomic_write_json(state_path(task_gid), state)
        if identity.dirty:
            fail("DIRTY_HANDOFF", "owned worktree/index is dirty; commit or deliberately preserve work before durable handoff")
        if relation != "equal" or remote_head != identity.head:
            fail(
                "HANDOFF_REMOTE_MISMATCH",
                f"remote owned branch must equal local HEAD before handoff; relation={relation}, remote={remote_head}, local={identity.head}",
            )
        payload = payload_from_state("verify-handoff", state, identity, relation=relation, remote_head=remote_head, target_head=target_head)
        payload.update(
            {
                "authoring_base_head": state["base_sha"],
                "local_implementation_head": identity.head,
                "remote_owned_head": remote_head,
                "current_target_head": target_head,
                "target_moved": moved,
            }
        )
        return payload


def remote_contains_head(runner: GitRunner, repo: Repository, remote_head: str | None, local_head: str) -> bool:
    if remote_head is None:
        return False
    if remote_head == local_head:
        return True
    ensure_commit_object(runner, repo, remote_head)
    result = runner.run(repo.source_top, "merge-base", "--is-ancestor", local_head, remote_head, check=False)
    if result.returncode not in (0, 1):
        fail("ANCESTRY_CHECK_FAILED", "could not verify remote recoverability")
    return result.returncode == 0


def command_cleanup(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    if args.disposition not in DISPOSITIONS:
        fail("INVALID_DISPOSITION", "cleanup disposition must be merged|closed|abandoned|superseded")
    with TaskLock(task_gid):
        state = load_task_state(task_gid)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        if identity.dirty:
            fail("DIRTY_CLEANUP", "cleanup refuses a dirty owned worktree/index")
        relation, remote_head = remote_relation(runner, repo, state["branch"], identity.head)
        if relation == "remote-ahead":
            fail("REMOTE_AHEAD", "cleanup refuses an owned branch whose remote head is ahead; explicit recovery is required")
        if relation == "divergent":
            fail("REMOTE_DIVERGED", "cleanup refuses a divergent owned branch; explicit recovery is required")
        target_head = remote_ref_sha(runner, repo, state["base_ref"])
        assert target_head is not None
        target_contains = remote_contains_head(runner, repo, target_head, identity.head)
        owned_contains = remote_contains_head(runner, repo, remote_head, identity.head)
        if not owned_contains and not (args.disposition == "merged" and target_contains):
            fail(
                "ONLY_RECOVERY_COPY",
                "cleanup refuses because the local implementation HEAD is not recoverable from the remote owned branch"
                + (" or current target branch" if args.disposition == "merged" else ""),
            )
        # The worktree was created locked by this tool. Unlock only immediately
        # before a non-force remove. If removal fails, best-effort re-lock it.
        unlock = runner.run(repo.source_top, "worktree", "unlock", str(identity.path), check=False)
        if unlock.returncode != 0:
            fail("CLEANUP_UNLOCK_FAILED", f"could not unlock owned worktree for cleanup: {unlock.stderr.strip()}")
        remove = runner.run(repo.source_top, "worktree", "remove", str(identity.path), check=False)
        if remove.returncode != 0:
            runner.run(
                repo.source_top,
                "worktree",
                "lock",
                "--reason",
                f"Dish task {task_gid}; cleanup remove failed",
                str(identity.path),
                check=False,
            )
            fail("CLEANUP_REMOVE_FAILED", f"non-force worktree removal failed: {remove.stderr.strip()}")
        state["lifecycle"] = args.disposition
        state["disposition"] = args.disposition
        state["disposed_at"] = now_utc()
        state["last_verified_at"] = now_utc()
        state["local_head"] = identity.head
        state["remote_owned_head"] = remote_head
        state["remote_relation"] = relation
        state["target_current_head"] = target_head
        atomic_write_json(state_path(task_gid), state)
        clear_agent_reference(owner_agent_id(state), task_gid)
        return {
            "command": "cleanup",
            "ok": True,
            "task_gid": task_gid,
            "disposition": args.disposition,
            "branch": state["branch"],
            "worktree": state["worktree_path"],
            "worktree_removed": True,
            "branch_retained": True,
            "local_head": identity.head,
            "remote_owned_head": remote_head,
            "current_target_head": target_head,
            "state_path": str(state_path(task_gid)),
        }


def command_exec(args: argparse.Namespace, runner: GitRunner) -> "NoReturn":
    task_gid = require_task_gid(args.task)
    state = load_task_state(task_gid)
    repo = resolve_repository_from_state(runner, state)
    identity = verify_owned_worktree(runner, repo, state)
    command = list(args.argv)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        fail("EXEC_COMMAND_REQUIRED", "exec requires a command after --")
    os.chdir(identity.path)
    runner.close()
    os.execvp(command[0], command)
    raise AssertionError("os.execvp returned unexpectedly")
