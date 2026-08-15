from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import GitRunner, fail, now_utc, require_agent_id, require_full_sha, require_task_gid, worktree_records, find_worktree_record
from .operations import branch_exists, owner_agent_id, remote_ref_sha, validate_base_ref, validate_branch, verify_owned_worktree
from .ownership import lineage_claim_locks, normalize_pr_identity, read_claim, remove_archived_claim
from .publish_cleanup import ignored_untracked_paths
from .repository import discover_repository
from .start_resume import _adopt_remote_branch_locked, _checked_out_path, _validate_adoption_remote
from .state import TaskLock, atomic_write_json, clear_agent_reference, load_task_state, set_agent_reference, state_path, task_worktree_path, validate_agent_state

MAX_PRIOR_LINEAGES = 32
SUPERSESSION_SCHEMA = "dish-task-lineage-supersession-v1"


def _required_text(value: str | None, flag: str) -> str:
    text = str(value or "").strip()
    if not text:
        fail("SUPERSESSION_PROVENANCE_REQUIRED", f"{flag} is required for deliberate task-lineage supersession")
    return text


def _identity(
    *,
    task_gid: str,
    old_state: dict[str, Any],
    old_branch: str,
    old_head: str,
    replacement_branch: str,
    replacement_head: str,
    replacement_base_ref: str,
    replacement_base_sha: str,
    pr: dict[str, Any],
    reason: str,
    provenance: str,
) -> dict[str, Any]:
    return {
        "schema": SUPERSESSION_SCHEMA,
        "repository": old_state["repository"],
        "task_gid": task_gid,
        "old": {
            "branch": old_branch,
            "head": old_head,
            "base_ref": old_state["base_ref"],
            "base_sha": old_state["base_sha"],
        },
        "replacement": {
            "branch": replacement_branch,
            "head": replacement_head,
            "base_ref": replacement_base_ref,
            "base_sha": replacement_base_sha,
            "pr": pr,
        },
        "reason": reason,
        "provenance": provenance,
    }


def _same_identity(stored: Any, requested: dict[str, Any]) -> None:
    if stored != requested:
        fail(
            "SUPERSESSION_IDENTITY_MISMATCH",
            "stored supersession identity differs from the requested task/old/replacement lineage; changed-identity retry refused",
        )


def _bind_repo(runner: GitRunner, args: argparse.Namespace, state: dict[str, Any]):
    repo = discover_repository(runner, Path(args.repo))
    if repo.common_dir != Path(str(state["git_common_dir"])).resolve():
        fail("COMMON_DIR_MISMATCH", "supersession repository common-dir differs from durable task state")
    if repo.origin_id != str(state["repository"]["origin_id"]):
        fail("ORIGIN_IDENTITY", "supersession repository origin differs from durable task state")
    return repo


def _verify_old_remote(runner: GitRunner, repo, old_branch: str, old_head: str) -> None:
    observed = remote_ref_sha(runner, repo, f"refs/heads/{old_branch}", allow_missing=True)
    if observed is None:
        fail(
            "ONLY_RECOVERY_COPY",
            "supersession refuses because the exact old implementation head is not recoverable from its remote branch",
        )
    if observed != old_head:
        fail(
            "EXPECTED_HEAD_MISMATCH",
            f"old remote branch moved or was misidentified: expected {old_head}, origin has {observed}; supersession refused",
        )


def _verify_old_worktree(runner: GitRunner, repo, state: dict[str, Any], old_head: str) -> bool:
    path = Path(str(state["worktree_path"])).resolve()
    record = find_worktree_record(worktree_records(runner, repo.source_top), path)
    exists = path.exists()
    if exists != (record is not None):
        fail("WORKTREE_AMBIGUOUS", "old task worktree filesystem/registry state disagrees during supersession")
    if not exists:
        return False
    identity = verify_owned_worktree(runner, repo, state)
    if identity.head != old_head:
        fail(
            "EXPECTED_HEAD_MISMATCH",
            f"old local owned HEAD {identity.head} != expected superseded head {old_head}; supersession refused",
        )
    if identity.dirty:
        fail("DIRTY_SUPERSESSION", "supersession refuses a dirty old owned worktree/index")
    ignored = ignored_untracked_paths(runner, identity.path)
    if ignored:
        preview = ", ".join(repr(item) for item in ignored[:3])
        suffix = "" if len(ignored) <= 3 else f" (+{len(ignored) - 3} more)"
        fail(
            "IGNORED_CONTENT_SUPERSESSION",
            "supersession refuses ignored task-local content omitted by normal Git status: " + preview + suffix,
        )
    return True


def _write_progress(task_gid: str, state: dict[str, Any], journal: dict[str, Any]) -> None:
    state["supersession"] = journal
    state["last_verified_at"] = now_utc()
    atomic_write_json(state_path(task_gid), state)


def _remove_old_local(
    runner: GitRunner,
    *,
    repo,
    task_gid: str,
    state: dict[str, Any],
    journal: dict[str, Any],
    old_branch: str,
    old_head: str,
) -> None:
    path = Path(str(state["worktree_path"])).resolve()
    worktree_exists = _verify_old_worktree(runner, repo, state, old_head)
    if worktree_exists:
        record = find_worktree_record(worktree_records(runner, repo.source_top), path)
        assert record is not None
        if "locked" in record:
            unlock = runner.run(repo.source_top, "worktree", "unlock", str(path), check=False)
            if unlock.returncode != 0:
                fail("SUPERSESSION_UNLOCK_FAILED", f"could not unlock old owned worktree: {unlock.stderr.strip()}")
        remove = runner.run(repo.source_top, "worktree", "remove", str(path), check=False)
        if remove.returncode != 0:
            runner.run(
                repo.source_top,
                "worktree",
                "lock",
                "--reason",
                f"Dish task {task_gid}; supersession remove failed",
                str(path),
                check=False,
            )
            fail("SUPERSESSION_REMOVE_FAILED", f"non-force old worktree removal failed: {remove.stderr.strip()}")
    if not journal.get("old_worktree_removed"):
        journal["old_worktree_removed"] = True
        journal["old_worktree_removed_at"] = now_utc()
        _write_progress(task_gid, state, journal)

    if branch_exists(runner, repo.source_top, old_branch):
        checked = _checked_out_path(runner, repo.source_top, old_branch)
        if checked is not None:
            fail("BRANCH_CHECKED_OUT", f"old local branch is still checked out at {checked}")
        actual = runner.sha(repo.source_top, f"refs/heads/{old_branch}")
        if actual != old_head:
            fail(
                "EXPECTED_HEAD_MISMATCH",
                f"old local branch moved after terminalization: expected {old_head}, local has {actual}; deletion refused",
            )
        deleted = runner.run(
            repo.source_top,
            "update-ref",
            "-d",
            f"refs/heads/{old_branch}",
            old_head,
            check=False,
        )
        if deleted.returncode != 0:
            fail("LOCAL_BRANCH_DELETE_FAILED", f"conditional old local branch deletion failed: {deleted.stderr.strip()}")
        if branch_exists(runner, repo.source_top, old_branch):
            fail("LOCAL_BRANCH_DELETE_VERIFY_FAILED", f"old local branch refs/heads/{old_branch} still exists after deletion")
    if not journal.get("old_local_branch_removed"):
        journal["old_local_branch_removed"] = True
        journal["old_local_branch_removed_at"] = now_utc()
        _write_progress(task_gid, state, journal)


def _completed_payload(state: dict[str, Any], journal: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
    replacement = journal["identity"]["replacement"]
    old = journal["identity"]["old"]
    return {
        "command": "supersede",
        "ok": True,
        "idempotent": idempotent,
        "task_gid": state["task_gid"],
        "old_branch": old["branch"],
        "old_head": old["head"],
        "branch": replacement["branch"],
        "expected_head": replacement["head"],
        "pr_number": replacement["pr"]["number"],
        "pr_head": replacement["pr"]["head"],
        "lifecycle": state["lifecycle"],
        "worktree": state["worktree_path"],
        "state_path": str(state_path(str(state["task_gid"]))),
        "supersession": journal,
    }


def command_supersede(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    agent_id = require_agent_id(args.agent_id)
    if agent_id is None:
        fail("OWNERSHIP_AGENT_REQUIRED", "supersession requires --agent-id for the replacement active lineage")
    validate_agent_state(agent_id)
    old_head = require_full_sha(args.old_head, "expected old lineage head")
    replacement_head = require_full_sha(args.expected_head, "expected replacement remote branch HEAD")
    replacement_base_sha = require_full_sha(args.base, "replacement supplied base SHA")
    reason = _required_text(args.reason, "--reason")
    provenance = _required_text(args.provenance, "--provenance")
    pr = normalize_pr_identity(args.pr_number, args.pr_head, args.pr_lease_state, args.pr_lease_id, required=True)
    assert pr is not None
    if pr["head"] != replacement_head:
        fail("SUPERSESSION_IDENTITY_MISMATCH", "replacement --expected-head must equal the exact supplied PR head")
    if pr["lease_state"] == "active":
        fail("SUPERSESSION_LIVE_PR_LEASE", "replacement PR has a visible active agent lease; supersession refuses competing live ownership")

    repo = discover_repository(runner, Path(args.repo))
    old_branch = validate_branch(runner, repo.source_top, args.old_branch)
    replacement_branch = validate_branch(runner, repo.source_top, args.branch)
    if old_branch == replacement_branch:
        fail("SUPERSESSION_IDENTITY_MISMATCH", "old and replacement branch must be different explicit lineages")
    replacement_base_ref = validate_base_ref(runner, repo.source_top, args.base_ref)

    locks = lineage_claim_locks(task_gid, [old_branch, replacement_branch], [int(pr["number"])])
    locks.acquire()
    try:
        with TaskLock(task_gid):
            state = load_task_state(task_gid)
            repo = _bind_repo(runner, args, state)
            journal = state.get("supersession")

            if state.get("lifecycle") == "active" and state.get("branch") == replacement_branch:
                if not isinstance(journal, dict) or journal.get("phase") != "complete":
                    fail("SUPERSESSION_PROVENANCE_MISSING", "replacement lineage is active without completed supersession provenance")
                requested = _identity(
                    task_gid=task_gid,
                    old_state={
                        **state,
                        "base_ref": journal["identity"]["old"]["base_ref"],
                        "base_sha": journal["identity"]["old"]["base_sha"],
                    },
                    old_branch=old_branch,
                    old_head=old_head,
                    replacement_branch=replacement_branch,
                    replacement_head=replacement_head,
                    replacement_base_ref=replacement_base_ref,
                    replacement_base_sha=replacement_base_sha,
                    pr=pr,
                    reason=reason,
                    provenance=provenance,
                )
                _same_identity(journal.get("identity"), requested)
                return _completed_payload(state, journal, idempotent=True)

            if state.get("lifecycle") == "active":
                if state.get("branch") != old_branch:
                    fail(
                        "SUPERSESSION_IDENTITY_MISMATCH",
                        f"durable task state owns {state.get('branch')!r}, not explicit old branch {old_branch!r}",
                    )
                repo = _bind_repo(runner, args, state)
                if str(state.get("local_head")) != old_head:
                    fail(
                        "EXPECTED_HEAD_MISMATCH",
                        f"durable old local head {state.get('local_head')} != explicit expected old head {old_head}",
                    )
                requested = _identity(
                    task_gid=task_gid,
                    old_state=state,
                    old_branch=old_branch,
                    old_head=old_head,
                    replacement_branch=replacement_branch,
                    replacement_head=replacement_head,
                    replacement_base_ref=replacement_base_ref,
                    replacement_base_sha=replacement_base_sha,
                    pr=pr,
                    reason=reason,
                    provenance=provenance,
                )

                _verify_old_remote(runner, repo, old_branch, old_head)
                _verify_old_worktree(runner, repo, state, old_head)
                _validate_adoption_remote(
                    runner,
                    repo=repo,
                    branch=replacement_branch,
                    base_ref=replacement_base_ref,
                    base_sha=replacement_base_sha,
                    expected_head=replacement_head,
                )
                if branch_exists(runner, repo.source_top, replacement_branch):
                    checked = _checked_out_path(runner, repo.source_top, replacement_branch)
                    detail = f" checked out at {checked}" if checked is not None else ""
                    fail("BRANCH_COLLISION", f"replacement branch already exists locally before supersession{detail}")

                archived_claim = read_claim(task_gid)
                if archived_claim is not None and archived_claim.get("branch") != old_branch:
                    fail("OWNERSHIP_AMBIGUOUS", "durable claim branch does not match the explicit old lineage")
                prior = state.get("prior_lineages")
                if prior is None:
                    prior = []
                if not isinstance(prior, list):
                    fail("STATE_INVALID", "prior_lineages must be an array when present")
                if len(prior) >= MAX_PRIOR_LINEAGES:
                    fail("SUPERSESSION_HISTORY_FULL", "bounded prior-lineage history is full; refusing to discard provenance")
                terminal_at = now_utc()
                history = {
                    "schema": SUPERSESSION_SCHEMA,
                    "disposition": "superseded",
                    "disposed_at": terminal_at,
                    "reason": reason,
                    "provenance": provenance,
                    "branch": old_branch,
                    "terminal_head": old_head,
                    "base_ref": state["base_ref"],
                    "base_sha": state["base_sha"],
                    "owner": state.get("owner"),
                    "owner_history": state.get("owner_history", []),
                    "claim": archived_claim,
                    "replacement": requested["replacement"],
                }
                state["prior_lineages"] = [*prior, history]
                journal = {
                    "schema": SUPERSESSION_SCHEMA,
                    "phase": "terminalized",
                    "identity": requested,
                    "started_at": terminal_at,
                    "terminalized_at": terminal_at,
                    "old_worktree_removed": False,
                    "old_local_branch_removed": False,
                    "replacement_activated": False,
                }
                state["lifecycle"] = "supersession-incomplete"
                state["disposition"] = "superseded"
                state["disposed_at"] = terminal_at
                _write_progress(task_gid, state, journal)
                clear_agent_reference(owner_agent_id(state), task_gid)
                remove_archived_claim(task_gid, archived_claim, old_branch)
            elif state.get("lifecycle") == "supersession-incomplete":
                if not isinstance(journal, dict) or journal.get("schema") != SUPERSESSION_SCHEMA:
                    fail("SUPERSESSION_PROVENANCE_MISSING", "incomplete supersession lacks a valid transition journal")
                requested = dict(journal.get("identity") or {})
                old_identity = requested.get("old") if isinstance(requested, dict) else None
                if not isinstance(old_identity, dict):
                    fail("SUPERSESSION_PROVENANCE_MISSING", "incomplete supersession journal has no old-lineage identity")
                old_state_for_identity = {
                    **state,
                    "base_ref": old_identity.get("base_ref"),
                    "base_sha": old_identity.get("base_sha"),
                }
                caller_identity = _identity(
                    task_gid=task_gid,
                    old_state=old_state_for_identity,
                    old_branch=old_branch,
                    old_head=old_head,
                    replacement_branch=replacement_branch,
                    replacement_head=replacement_head,
                    replacement_base_ref=replacement_base_ref,
                    replacement_base_sha=replacement_base_sha,
                    pr=pr,
                    reason=reason,
                    provenance=provenance,
                )
                _same_identity(requested, caller_identity)
                prior = state.get("prior_lineages")
                archived_claim = None
                if isinstance(prior, list) and prior:
                    last = prior[-1]
                    if isinstance(last, dict) and last.get("branch") == old_branch and last.get("terminal_head") == old_head:
                        claim_value = last.get("claim")
                        archived_claim = claim_value if isinstance(claim_value, dict) else None
                remove_archived_claim(task_gid, archived_claim, old_branch)
                old_owner = None
                if isinstance(prior, list) and prior and isinstance(prior[-1], dict):
                    owner_value = prior[-1].get("owner")
                    if isinstance(owner_value, dict) and owner_value.get("agent_id") is not None:
                        old_owner = str(owner_value["agent_id"])
                clear_agent_reference(old_owner, task_gid)
            else:
                fail(
                    "TASK_NOT_ACTIVE",
                    f"task lifecycle is {state.get('lifecycle')!r}; supersession requires the exact active old lineage or its own incomplete journal",
                )

            assert isinstance(journal, dict)
            identity = journal["identity"]
            _verify_old_remote(runner, repo, old_branch, old_head)
            observed_replacement = remote_ref_sha(runner, repo, f"refs/heads/{replacement_branch}", allow_missing=True)
            if observed_replacement != replacement_head:
                fail(
                    "EXPECTED_HEAD_MISMATCH",
                    f"replacement remote branch moved during supersession: expected {replacement_head}, origin has {observed_replacement}",
                )

            _remove_old_local(
                runner,
                repo=repo,
                task_gid=task_gid,
                state=state,
                journal=journal,
                old_branch=old_branch,
                old_head=old_head,
            )

            if not journal.get("replacement_activation_started"):
                journal["replacement_activation_started"] = True
                journal["replacement_activation_started_at"] = now_utc()
                _write_progress(task_gid, state, journal)

            provisional, worktree_identity, current_target = _adopt_remote_branch_locked(
                runner,
                task_gid=task_gid,
                agent_id=agent_id,
                repo=repo,
                branch=replacement_branch,
                base_ref=replacement_base_ref,
                base_sha=replacement_base_sha,
                expected_head=replacement_head,
                allow_exact_local_retry=True,
            )
            journal["phase"] = "complete"
            journal["replacement_activated"] = True
            journal["replacement_activated_at"] = journal.get("replacement_activated_at") or now_utc()
            journal["completed_at"] = journal.get("completed_at") or now_utc()
            provisional["prior_lineages"] = state.get("prior_lineages", [])
            provisional["supersession"] = journal
            provisional["pr_url"] = f"https://github.com/marcogallotta/ai-tools/pull/{pr['number']}"
            provisional["pr_head"] = pr["head"]
            provisional["target_current_head"] = current_target
            atomic_write_json(state_path(task_gid), provisional)
            set_agent_reference(agent_id, provisional)
            return _completed_payload(provisional, journal, idempotent=False)
    finally:
        locks.release()
