from __future__ import annotations

import argparse
import os
import uuid
from typing import Any, NoReturn

from .common import DISPOSITIONS, GitRunner, fail, now_utc, require_task_gid
from .operations import (
    payload_from_state, record_observation, remote_and_target_observation, remote_ref_sha,
    remote_relation, resolve_repository_from_state, verify_owned_worktree, ensure_commit_object,
)
from .state import TaskLock, atomic_write_json, clear_agent_reference, load_task_state, state_path
from .operations import owner_agent_id
from .ownership import require_active_claim, read_claim
from . import global_claim

def _claimed_state(task_gid: str) -> dict[str, Any]:
    state = load_task_state(task_gid)
    claimed_agent = require_active_claim(task_gid, str(state["branch"]), owner_agent_id(state))["agent_id"]
    if owner_agent_id(state) is None:
        # A legacy ownerless active state may be explicitly claimed only through
        # dispatch takeover/resume; writer-only operations must not silently
        # assign provenance while bypassing that reconciliation boundary.
        fail("OWNER_MISMATCH", f"task record has no concrete owner; claimed agent {claimed_agent!r} must resume --takeover first")
    return state


def command_publish(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    _claimed_state(task_gid)
    with TaskLock(task_gid):
        state = _claimed_state(task_gid)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        claim = read_claim(task_gid)
        if claim is None or not claim.get("global_claim_id"):
            fail("GLOBAL_CLAIM_REQUIRED", "publish requires a durable global claim bound to the local ownership record")
        claim_id = str(claim["global_claim_id"])
        branch = str(state["branch"])
        relation, remote_head = remote_relation(runner, repo, branch, identity.head)
        if relation == "remote-ahead":
            fail("REMOTE_AHEAD", "remote owned branch is ahead; publish refuses automatic synchronization")
        if relation == "divergent":
            fail("REMOTE_DIVERGED", "remote owned branch diverged; publish refuses merge/rebase/force-push")

        durable = global_claim.authorize(task_gid, claim_id, branch)
        if relation == "equal":
            if durable.get("branch_head") != identity.head:
                fail(
                    "GLOBAL_HEAD_UNRECORDED",
                    f"remote branch already equals local HEAD {identity.head}, but durable claim records {durable.get('branch_head')!r}; explicit lineage reconciliation is required",
                )
        else:
            pending = state.get("global_publication")
            if isinstance(pending, dict):
                if pending.get("claim_id") != claim_id or pending.get("proposed_head") != identity.head:
                    fail("GLOBAL_PUBLICATION_AMBIGUOUS", "stored publication intent does not match the current claim/local HEAD")
                request_id = str(pending.get("request_id") or "")
                expected_head = pending.get("expected_head")
                observed = remote_ref_sha(runner, repo, f"refs/heads/{branch}", allow_missing=True)
                if observed == identity.head:
                    global_claim.complete_publication(
                        task_gid=task_gid, claim_id=claim_id, request_id=request_id, result_head=identity.head
                    )
                    state.pop("global_publication", None)
                    atomic_write_json(state_path(task_gid), state)
                    relation, remote_head = "equal", identity.head
                elif observed != expected_head:
                    fail(
                        "HEAD_MOVED",
                        f"pending publication expected remote {expected_head!r} or proposed {identity.head}, observed {observed!r}",
                    )
            else:
                request_id = f"agent-worktree-{uuid.uuid4().hex}"
                expected_head = remote_head
                state["global_publication"] = {
                    "request_id": request_id,
                    "claim_id": claim_id,
                    "expected_head": expected_head,
                    "proposed_head": identity.head,
                    "started_at": now_utc(),
                }
                atomic_write_json(state_path(task_gid), state)

            if relation != "equal":
                pending = state["global_publication"]
                request_id = str(pending["request_id"])
                expected_head = pending.get("expected_head")
                global_claim.begin_publication(
                    task_gid=task_gid, claim_id=claim_id, branch=branch, expected_head=expected_head,
                    proposed_head=identity.head, request_id=request_id,
                )
                ref = f"refs/heads/{branch}"
                refspec = f"{ref}:{ref}"
                result = runner.run(repo.source_top, "push", repo.origin_url, refspec, check=False)
                verified_remote = remote_ref_sha(runner, repo, ref, allow_missing=True)
                if verified_remote == identity.head:
                    global_claim.complete_publication(
                        task_gid=task_gid, claim_id=claim_id, request_id=request_id, result_head=identity.head
                    )
                    state.pop("global_publication", None)
                elif verified_remote == expected_head:
                    # Keep the exact publication intent durable. A retry reuses the same request id;
                    # do not create a competing request after an unambiguous no-move failure.
                    atomic_write_json(state_path(task_gid), state)
                    fail("PUBLISH_FAILED", f"explicit owned-branch push failed without branch movement: {result.stderr.strip()}")
                else:
                    atomic_write_json(state_path(task_gid), state)
                    fail(
                        "HEAD_MOVED",
                        f"publication branch moved to {verified_remote!r}, neither expected {expected_head!r} nor proposed {identity.head}",
                    )
                remote_head = verified_remote
                relation = "equal"

        verified_remote = remote_ref_sha(runner, repo, f"refs/heads/{branch}")
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
    _claimed_state(task_gid)
    with TaskLock(task_gid):
        state = _claimed_state(task_gid)
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


def remote_contains_head(runner: GitRunner, repo, remote_head: str | None, local_head: str) -> bool:
    if remote_head is None:
        return False
    if remote_head == local_head:
        return True
    ensure_commit_object(runner, repo, remote_head)
    result = runner.run(repo.source_top, "merge-base", "--is-ancestor", local_head, remote_head, check=False)
    if result.returncode not in (0, 1):
        fail("ANCESTRY_CHECK_FAILED", "could not verify remote recoverability")
    return result.returncode == 0


def ignored_untracked_paths(runner: GitRunner, worktree) -> list[str]:
    result = runner.run(
        worktree,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    )
    return [path for path in result.stdout.split("\0") if path]


def command_cleanup(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    if args.disposition not in DISPOSITIONS:
        fail("INVALID_DISPOSITION", "cleanup disposition must be merged|closed|abandoned|superseded")
    _claimed_state(task_gid)
    with TaskLock(task_gid):
        state = _claimed_state(task_gid)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        if identity.dirty:
            fail("DIRTY_CLEANUP", "cleanup refuses a dirty owned worktree/index")
        ignored = ignored_untracked_paths(runner, identity.path)
        if ignored:
            preview = ", ".join(repr(path) for path in ignored[:3])
            suffix = "" if len(ignored) <= 3 else f" (+{len(ignored) - 3} more)"
            fail(
                "IGNORED_CONTENT_CLEANUP",
                "cleanup refuses ignored task-local content omitted by normal Git status: " + preview + suffix,
            )
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
    state = _claimed_state(task_gid)
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
