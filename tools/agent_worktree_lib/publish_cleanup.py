from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any, NoReturn

from .common import (
    DISPOSITIONS, GitRunner, fail, find_worktree_record, now_utc, require_full_sha,
    require_task_gid, worktree_records,
)
from .operations import (
    branch_exists, payload_from_state, record_observation, remote_and_target_observation, remote_ref_sha,
    remote_relation, resolve_repository_from_state, validate_branch, verify_owned_worktree, ensure_commit_object,
)
from .repository import discover_repository
from .state import TaskLock, atomic_write_json, clear_agent_reference, load_task_state, read_json_object, state_path, state_path_for_branch
from .operations import owner_agent_id
from .ownership import invalidate_claim_after_head_movement, require_active_claim
from .lineage import LINEAGE_ENV, record_lineage_head, terminalize_lineage

def _claimed_state(task_gid: str, runner: GitRunner, *, allow_head_moved_readback: bool = False) -> dict[str, Any]:
    state = load_task_state(task_gid)
    claimed_agent = require_active_claim(
        task_gid,
        str(state["branch"]),
        owner_agent_id(state),
        runner,
        allow_head_moved_readback=allow_head_moved_readback,
    )["agent_id"]
    if owner_agent_id(state) is None:
        # A legacy ownerless active state may be explicitly claimed only through
        # dispatch takeover/resume; writer-only operations must not silently
        # assign provenance while bypassing that reconciliation boundary.
        fail("OWNER_MISMATCH", f"task record has no concrete owner; claimed agent {claimed_agent!r} must resume --takeover first")
    return state


def command_publish(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    _claimed_state(task_gid, runner)
    with TaskLock(task_gid):
        state = _claimed_state(task_gid, runner)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        relation, remote_head = remote_relation(runner, repo, state["branch"], identity.head)
        if relation == "remote-ahead":
            fail("REMOTE_AHEAD", "remote owned branch is ahead; publish refuses automatic synchronization")
        if relation == "divergent":
            fail("REMOTE_DIVERGED", "remote owned branch diverged; publish refuses merge/rebase/force-push")
        first_publication = state.get("remote_owned_head") is None and state.get("published_head") is None
        if first_publication and relation != "absent":
            # A new lineage is admitted before its first remote branch attachment.
            # If the branch appears before that attachment attempt, do not silently
            # adopt it merely because it happens to point at the same SHA. Supported
            # agent-worktree contenders are already serialized by registry CAS; this
            # check fences local-vs-out-of-band branch creation races.
            fail(
                "BRANCH_CREATE_RACE",
                f"remote branch {state['branch']!r} appeared before first lineage publication at {remote_head}; exact admitted lineage will not adopt it",
            )
        if relation != "equal":
            ref = f"refs/heads/{state['branch']}"
            refspec = f"{ref}:{ref}"
            push_args = ["push"]
            if first_publication:
                push_args.append(f"--force-with-lease={ref}:")
            push_args += [repo.origin_url, refspec]
            result = runner.run(repo.source_top, *push_args, check=False)
            if result.returncode != 0:
                observed = remote_ref_sha(runner, repo, ref, allow_missing=True)
                if first_publication and observed is not None:
                    fail("BRANCH_CREATE_RACE", f"remote branch {state['branch']!r} was created concurrently before expected-absent attachment completed; observed {observed}")
                fail("PUBLISH_FAILED", f"explicit owned-branch push failed without force: {result.stderr.strip()}")
        verified_remote = remote_ref_sha(runner, repo, f"refs/heads/{state['branch']}")
        assert verified_remote is not None
        if verified_remote != identity.head:
            fail("PUBLISH_VERIFY_FAILED", f"remote owned head {verified_remote} != local HEAD {identity.head}")
        lineage_id = state.get("lineage_id") or os.environ.get(LINEAGE_ENV)
        token = os.environ.get("DISH_AGENT_CLAIM_TOKEN")
        if lineage_id and token:
            record_lineage_head(
                runner, repo, task_gid=task_gid, branch=str(state["branch"]),
                lineage_id=str(lineage_id), token=token, head=verified_remote,
            )
        state["published_head"] = identity.head
        state["remote_owned_head"] = verified_remote
        state["remote_relation"] = "equal"
        state["remote_checked_at"] = now_utc()
        state["last_verified_at"] = now_utc()
        state["local_head"] = identity.head
        atomic_write_json(state_path(task_gid), state)
        moved_pr_head = invalidate_claim_after_head_movement(task_gid, verified_remote)
        payload = payload_from_state("publish", state, identity, relation="equal", remote_head=verified_remote)
        payload["head_moved_requires_redispatch"] = moved_pr_head
        return payload


def command_verify_handoff(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    _claimed_state(task_gid, runner, allow_head_moved_readback=True)
    with TaskLock(task_gid):
        state = _claimed_state(task_gid, runner, allow_head_moved_readback=True)
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


def _checked_out_path(runner: GitRunner, repo, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    for record in worktree_records(runner, repo.source_top):
        if record.get("branch") == ref:
            return record.get("worktree") or "<unknown>"
    return None


def _write_cleanup_progress(task_gid: str, state: dict[str, Any], cleanup: dict[str, Any]) -> None:
    state["terminal_cleanup"] = cleanup
    state["last_verified_at"] = now_utc()
    lineage_id = state.get("lineage_id")
    path = state_path(task_gid, str(state["branch"]), str(lineage_id)) if lineage_id else state_path(task_gid)
    atomic_write_json(path, state)


def _delete_remote_branch_exact(runner: GitRunner, repo, branch: str, expected_head: str) -> None:
    ref = f"refs/heads/{branch}"
    current = remote_ref_sha(runner, repo, ref, allow_missing=True)
    if current is None:
        return
    if current != expected_head:
        fail(
            "EXPECTED_HEAD_MISMATCH",
            f"remote terminal branch moved or was reused: expected {expected_head}, origin has {current}; deletion refused",
        )
    result = runner.run(
        repo.source_top,
        "push",
        f"--force-with-lease={ref}:{expected_head}",
        repo.origin_url,
        f":{ref}",
        check=False,
    )
    if result.returncode != 0:
        observed = remote_ref_sha(runner, repo, ref, allow_missing=True)
        if observed is None:
            return
        if observed != expected_head:
            fail(
                "EXPECTED_HEAD_MISMATCH",
                f"remote terminal branch moved during deletion: expected {expected_head}, origin has {observed}; deletion refused",
            )
        detail = result.stderr.strip() or result.stdout.strip() or "no command output"
        fail("REMOTE_DELETE_FAILED", f"expected-head remote branch deletion failed: {detail}")
    observed = remote_ref_sha(runner, repo, ref, allow_missing=True)
    if observed is not None:
        fail("REMOTE_DELETE_VERIFY_FAILED", f"remote branch {ref} still exists at {observed} after deletion")


def command_cleanup(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    if args.disposition not in DISPOSITIONS:
        fail("INVALID_DISPOSITION", "cleanup disposition must be merged|closed|abandoned|superseded")
    requested_state = state_path_for_branch(task_gid, args.branch) if args.branch else None
    if args.branch is None:
        try:
            candidate = state_path(task_gid)
            requested_state = candidate if candidate.exists() else None
        except Exception as exc:
            if getattr(exc, "code", None) == "LINEAGE_AMBIGUOUS":
                fail("LINEAGE_AMBIGUOUS", "cleanup for a task with multiple lineages requires --branch")
            raise
    state = read_json_object(requested_state, "task worktree state") if requested_state is not None else None
    lineage_id = str(state.get("lineage_id")) if state is not None and state.get("lineage_id") else None
    with TaskLock(task_gid, args.branch or (str(state.get("branch")) if state else None), lineage_id):
        state_file = requested_state
        if state is not None:
            # Terminal cleanup must remain restartable after lifecycle leaves active.
            # Resolve from the explicitly supplied controller checkout, then bind it
            # back to the durable task record instead of using active-only helpers.
            repo = discover_repository(runner, Path(args.repo))
            if repo.common_dir != Path(str(state["git_common_dir"])).resolve():
                fail("COMMON_DIR_MISMATCH", "terminal cleanup repository common-dir differs from task state")
            if repo.origin_id != str(state["repository"]["origin_id"]):
                fail("ORIGIN_IDENTITY", "terminal cleanup repository origin differs from task state")
            branch = validate_branch(runner, repo.source_top, args.branch or str(state["branch"]))
            if state["branch"] != branch:
                fail("BRANCH_MISMATCH", f"task state owns {state['branch']!r}, terminal PR identifies {branch!r}")
            if args.expected_head:
                expected_head = require_full_sha(args.expected_head, "terminal expected head")
            elif branch_exists(runner, repo.source_top, branch):
                expected_head = require_full_sha(runner.sha(repo.source_top, f"refs/heads/{branch}"), "terminal expected head")
            else:
                expected_head = require_full_sha(str(state["local_head"]), "terminal expected head")
            pr_number = args.pr_number
            if pr_number is None:
                stored_url = state.get("pr_url")
                match = re.search(r"/pull/(\d+)(?:$|[/?#])", str(stored_url or ""))
                pr_number = int(match.group(1)) if match else 0
        else:
            if not args.branch or not args.expected_head or args.pr_number is None:
                fail("TERMINAL_IDENTITY_REQUIRED", "cleanup without local task state requires --branch, --expected-head, and --pr-number")
            repo = discover_repository(runner, Path(args.repo))
            branch = validate_branch(runner, repo.source_top, args.branch)
            expected_head = require_full_sha(args.expected_head, "terminal expected head")
            pr_number = args.pr_number
        if pr_number is not None and pr_number < 0:
            fail("INVALID_PR", "terminal PR number must be non-negative")

        remote_ref = f"refs/heads/{branch}"
        remote_head = remote_ref_sha(runner, repo, remote_ref, allow_missing=True)
        if args.expected_head and remote_head is not None and remote_head != expected_head:
            fail(
                "EXPECTED_HEAD_MISMATCH",
                f"remote terminal branch moved or was reused: expected {expected_head}, origin has {remote_head}; cleanup refused",
            )

        worktree_removed = False
        local_branch_removed = False
        cleanup: dict[str, Any] | None = None
        local_head = expected_head
        target_head = None

        if state is not None:
            prior_cleanup = state.get("terminal_cleanup")
            if prior_cleanup is not None:
                if not isinstance(prior_cleanup, dict):
                    fail("STATE_INVALID", "terminal_cleanup state must be an object")
                if prior_cleanup.get("expected_head") != expected_head or prior_cleanup.get("branch") != branch:
                    fail("CLEANUP_IDENTITY_MISMATCH", "stored terminal cleanup identity differs from requested PR branch/head")
                if prior_cleanup.get("pr_number") != pr_number or prior_cleanup.get("disposition") != args.disposition:
                    fail("CLEANUP_IDENTITY_MISMATCH", "stored terminal cleanup PR/disposition differs from requested terminal identity")
                cleanup = dict(prior_cleanup)

            worktree_path = Path(str(state["worktree_path"]))
            record = find_worktree_record(worktree_records(runner, repo.source_top), worktree_path)
            worktree_exists = worktree_path.exists()
            if worktree_exists or record is not None:
                if not worktree_exists or record is None:
                    fail("WORKTREE_AMBIGUOUS", "task worktree filesystem/registry state disagrees during terminal cleanup")
                identity = verify_owned_worktree(runner, repo, state)
                local_head = identity.head
                if identity.head != expected_head:
                    fail(
                        "EXPECTED_HEAD_MISMATCH",
                        f"local owned HEAD {identity.head} != terminal PR head {expected_head}; cleanup refused",
                    )
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
            elif cleanup is None and state.get("lifecycle") == "active":
                fail("WORKTREE_MISSING", "active task state points to a missing worktree before terminal cleanup started")

            base_ref = str(state["base_ref"])
            target_head = remote_ref_sha(runner, repo, base_ref)
            assert target_head is not None
            target_contains = remote_contains_head(runner, repo, target_head, expected_head)
            remote_contains = remote_contains_head(runner, repo, remote_head, expected_head)
            cleanup_remote_deleted = bool(cleanup and cleanup.get("remote_branch_removed"))
            if not remote_contains and not cleanup_remote_deleted and not (args.disposition == "merged" and target_contains):
                fail(
                    "ONLY_RECOVERY_COPY",
                    "cleanup refuses because the terminal implementation head is not recoverable from the remote owned branch"
                    + (" or current target branch" if args.disposition == "merged" else ""),
                )

            if cleanup is None:
                cleanup = {
                    "schema": "dish-terminal-cleanup-v1",
                    "task_gid": task_gid,
                    "pr_number": pr_number,
                    "disposition": args.disposition,
                    "branch": branch,
                    "expected_head": expected_head,
                    "started_at": now_utc(),
                    "worktree_removed": False,
                    "local_branch_removed": False,
                    "remote_branch_removed": False,
                    "complete": False,
                }
                state["lifecycle"] = args.disposition
                state["disposition"] = args.disposition
                state["disposed_at"] = state.get("disposed_at") or now_utc()
                if lineage_id is not None:
                    cleanup["registry_tombstone"] = terminalize_lineage(
                        runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id,
                        disposition=args.disposition, expected_head=expected_head,
                    )
                    cleanup["registry_terminalized"] = True
                    cleanup["registry_terminalized_at"] = now_utc()
                _write_cleanup_progress(task_gid, state, cleanup)

            elif lineage_id is not None and not cleanup.get("registry_terminalized"):
                cleanup["registry_tombstone"] = terminalize_lineage(
                    runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id,
                    disposition=args.disposition, expected_head=expected_head,
                )
                cleanup["registry_terminalized"] = True
                cleanup["registry_terminalized_at"] = now_utc()
                _write_cleanup_progress(task_gid, state, cleanup)

            if worktree_exists:
                unlock = runner.run(repo.source_top, "worktree", "unlock", str(worktree_path), check=False)
                if unlock.returncode != 0:
                    fail("CLEANUP_UNLOCK_FAILED", f"could not unlock owned worktree for cleanup: {unlock.stderr.strip()}")
                remove = runner.run(repo.source_top, "worktree", "remove", str(worktree_path), check=False)
                if remove.returncode != 0:
                    runner.run(
                        repo.source_top,
                        "worktree",
                        "lock",
                        "--reason",
                        f"Dish task {task_gid}; cleanup remove failed",
                        str(worktree_path),
                        check=False,
                    )
                    fail("CLEANUP_REMOVE_FAILED", f"non-force worktree removal failed: {remove.stderr.strip()}")
                worktree_removed = True
                cleanup["worktree_removed"] = True
                cleanup["worktree_removed_at"] = now_utc()
                _write_cleanup_progress(task_gid, state, cleanup)
            else:
                # If a process died after Git removed the worktree but before the
                # journal fsync, authoritative registry/filesystem absence is the
                # readback for that completed step.
                worktree_removed = cleanup is not None
                if cleanup is not None and not cleanup.get("worktree_removed"):
                    cleanup["worktree_removed"] = True
                    cleanup["worktree_removed_at"] = cleanup.get("worktree_removed_at") or now_utc()
                    _write_cleanup_progress(task_gid, state, cleanup)

            if branch_exists(runner, repo.source_top, branch):
                checked = _checked_out_path(runner, repo, branch)
                if checked is not None:
                    fail("BRANCH_CHECKED_OUT", f"terminal local branch is still checked out at {checked}")
                local_actual = runner.sha(repo.source_top, f"refs/heads/{branch}")
                if local_actual != expected_head:
                    fail(
                        "EXPECTED_HEAD_MISMATCH",
                        f"local terminal branch moved or was reused: expected {expected_head}, local has {local_actual}; deletion refused",
                    )
                deleted = runner.run(repo.source_top, "update-ref", "-d", f"refs/heads/{branch}", expected_head, check=False)
                if deleted.returncode != 0:
                    fail("LOCAL_BRANCH_DELETE_FAILED", f"conditional local branch deletion failed: {deleted.stderr.strip()}")
                if branch_exists(runner, repo.source_top, branch):
                    fail("LOCAL_BRANCH_DELETE_VERIFY_FAILED", f"local branch refs/heads/{branch} still exists after deletion")
                local_branch_removed = True
                cleanup["local_branch_removed"] = True
                cleanup["local_branch_removed_at"] = now_utc()
                _write_cleanup_progress(task_gid, state, cleanup)
            else:
                local_branch_removed = cleanup is not None
                if cleanup is not None and not cleanup.get("local_branch_removed"):
                    cleanup["local_branch_removed"] = True
                    cleanup["local_branch_removed_at"] = cleanup.get("local_branch_removed_at") or now_utc()
                    _write_cleanup_progress(task_gid, state, cleanup)
        else:
            # A ChatGPT/native branch may have no local task-worktree record on this
            # controller host. Exact GitHub PR branch/head identity is sufficient for
            # remote agent/* cleanup, but absence of local state never authorizes
            # deleting an unrelated local ref/worktree cache.
            local_head = expected_head

        _delete_remote_branch_exact(runner, repo, branch, expected_head)
        remote_removed = remote_ref_sha(runner, repo, remote_ref, allow_missing=True) is None
        if not remote_removed:
            fail("REMOTE_DELETE_VERIFY_FAILED", f"remote branch {remote_ref} still exists after terminal cleanup")

        if state is not None:
            assert cleanup is not None
            cleanup["remote_branch_removed"] = True
            cleanup["remote_branch_removed_at"] = cleanup.get("remote_branch_removed_at") or now_utc()
            cleanup["complete"] = True
            cleanup["completed_at"] = cleanup.get("completed_at") or now_utc()
            state["local_head"] = expected_head
            state["remote_owned_head"] = None
            state["remote_relation"] = "missing"
            if target_head is not None:
                state["target_current_head"] = target_head
            _write_cleanup_progress(task_gid, state, cleanup)
            clear_agent_reference(owner_agent_id(state), task_gid, lineage_id)

        return {
            "command": "cleanup",
            "ok": True,
            "task_gid": task_gid,
            "pr_number": pr_number,
            "disposition": args.disposition,
            "branch": branch,
            "expected_head": expected_head,
            "worktree": str(state["worktree_path"]) if state is not None else None,
            "worktree_removed": worktree_removed,
            "local_branch_removed": local_branch_removed,
            "remote_branch_removed": remote_removed,
            "local_state_present": state is not None,
            "local_head": local_head,
            "remote_owned_head": None,
            "current_target_head": target_head,
            "state_path": str(requested_state) if requested_state is not None else None,
        }


def command_exec(args: argparse.Namespace, runner: GitRunner) -> "NoReturn":
    task_gid = require_task_gid(args.task)
    state = _claimed_state(task_gid, runner)
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
