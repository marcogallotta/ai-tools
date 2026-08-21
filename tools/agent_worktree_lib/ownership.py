from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .common import (
    EXPECTED_ORIGIN_ID,
    EXPECTED_REPOSITORY,
    GitRunner,
    fail,
    now_utc,
    require_agent_id,
    require_full_sha,
    require_task_gid,
)
from .operations import owner_agent_id, remote_ref_sha, resolve_repository_from_state, validate_branch, verify_owned_worktree
from .lineage import (
    LINEAGE_ENV, acquire_pr_registry_claim, assert_active_registry_claim, branch_digest, claim_registry_ownership, fence_lineage,
    read_registry, release_pr_registry_claim, release_registry_claim, reserve_or_recover_lineage, resolve_lineage_for_branch,
)
from .launch_identity import bind_identity_from_launch_provenance, restore_identity
from .repository import discover_repository
from .state import (
    atomic_write_json,
    load_task_state,
    read_json_object,
    state_path,
    state_path_for_branch,
    state_root,
    task_state_paths,
    validate_agent_state,
    require_repository_mutation_identity,
)

CLAIM_SCHEMA_VERSION = 2
CLAIM_TOKEN_ENV = "DISH_AGENT_CLAIM_TOKEN"
CLAIM_TASK_ENV = "DISH_AGENT_CLAIM_TASK"
CLAIM_BRANCH_ENV = "DISH_AGENT_CLAIM_BRANCH"
CLAIM_AGENT_ENV = "DISH_AGENT_CLAIM_AGENT"


def _repo_key() -> str:
    return hashlib.sha256(EXPECTED_REPOSITORY.encode("utf-8")).hexdigest()[:16]


def claim_root() -> Path:
    root = state_root() / "claims" / _repo_key()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def claim_path(task_gid: str, branch: str | None = None, lineage_id: str | None = None) -> Path:
    task_gid = require_task_gid(task_gid)
    if branch is None and os.environ.get(CLAIM_TASK_ENV) == task_gid:
        branch = os.environ.get(CLAIM_BRANCH_ENV)
        lineage_id = os.environ.get(LINEAGE_ENV)
    if branch is not None or lineage_id is not None:
        if branch is None or lineage_id is None:
            fail("LINEAGE_ID_INVALID", "claim path requires branch and lineage_id together")
        return claim_root() / f"{task_gid}-{branch_digest(branch)}-{lineage_id}.json"
    matches = sorted(claim_root().glob(f"{task_gid}-*.json"))
    legacy = claim_root() / f"{task_gid}.json"
    if legacy.exists():
        matches.insert(0, legacy)
    if len(matches) > 1:
        fail("LINEAGE_AMBIGUOUS", f"task {task_gid} has multiple durable ownership claims; exact lineage is required")
    return matches[0] if matches else legacy


def claim_paths(task_gid: str) -> list[Path]:
    task_gid = require_task_gid(task_gid)
    matches = sorted(claim_root().glob(f"{task_gid}-*.json"))
    legacy = claim_root() / f"{task_gid}.json"
    if legacy.exists():
        matches.insert(0, legacy)
    return matches


def task_claim_lock_path(task_gid: str) -> Path:
    return claim_root() / f"task-{require_task_gid(task_gid)}.lock"


def branch_claim_lock_path(branch: str) -> Path:
    digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:24]
    return claim_root() / f"branch-{digest}.lock"


def pr_claim_lock_path(pr_number: int) -> Path:
    return claim_root() / f"pr-{pr_number}.lock"


def _open_lock(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_close(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _lock_is_held(path: Path) -> bool:
    fd = _open_lock(path)
    try:
        if _try_lock(fd):
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        return True
    finally:
        os.close(fd)


def _validate_claim_record(record: dict[str, Any], task_gid: str) -> dict[str, Any]:
    required = {
        "schema_version",
        "repository",
        "origin_id",
        "task_gid",
        "branch",
        "agent_id",
        "host",
        "token",
        "acquired_at",
        "released_at",
        "takeover",
        "pr",
    }
    missing = sorted(required - record.keys())
    if missing:
        fail("OWNERSHIP_AMBIGUOUS", "claim record is missing required field(s): " + ", ".join(missing))
    if record.get("schema_version") not in {1, CLAIM_SCHEMA_VERSION}:
        fail("OWNERSHIP_AMBIGUOUS", f"unsupported claim schema {record.get('schema_version')!r}")
    if record.get("schema_version") == CLAIM_SCHEMA_VERSION:
        lineage_id = record.get("lineage_id")
        if not isinstance(lineage_id, str) or len(lineage_id) < 16:
            fail("OWNERSHIP_AMBIGUOUS", "claim record has invalid lineage_id")
    if record.get("repository") != EXPECTED_REPOSITORY or record.get("origin_id") != EXPECTED_ORIGIN_ID:
        fail("OWNERSHIP_AMBIGUOUS", "claim record repository identity is not marcogallotta/ai-tools")
    if record.get("task_gid") != task_gid:
        fail("OWNERSHIP_AMBIGUOUS", "claim record task identity does not match its filename")
    agent_id = require_agent_id(record.get("agent_id"))
    if agent_id is None:
        fail("OWNERSHIP_AMBIGUOUS", "claim record has no concrete local agent owner")
    branch = str(record.get("branch"))
    if not branch.startswith("agent/"):
        fail("OWNERSHIP_AMBIGUOUS", f"claim record has invalid branch {branch!r}")
    token = record.get("token")
    if not isinstance(token, str) or len(token) < 16:
        fail("OWNERSHIP_AMBIGUOUS", "claim record token is invalid")
    pr = record.get("pr")
    if pr is not None:
        if not isinstance(pr, dict):
            fail("OWNERSHIP_AMBIGUOUS", "claim PR identity must be an object or null")
        if not isinstance(pr.get("number"), int) or pr["number"] <= 0:
            fail("OWNERSHIP_AMBIGUOUS", "claim PR number is invalid")
        require_full_sha(str(pr.get("head")), "claim PR head")
        if pr.get("lease_state") not in {"active", "none"}:
            fail("OWNERSHIP_AMBIGUOUS", "claim PR lease_state must be active or none")
        if pr.get("lease_state") == "active" and not pr.get("lease_id"):
            fail("OWNERSHIP_AMBIGUOUS", "active PR lease visibility requires a lease id")
    return record


def read_claim(task_gid: str, branch: str | None = None, lineage_id: str | None = None) -> dict[str, Any] | None:
    path = claim_path(task_gid, branch, lineage_id)
    if not path.exists():
        return None
    return _validate_claim_record(read_json_object(path, "local-agent ownership claim"), task_gid)


def _claim_status_record(task_gid: str, record: dict[str, Any], path: Path) -> dict[str, Any]:
    lock_paths = [branch_claim_lock_path(str(record["branch"]))]
    pr = record.get("pr")
    if isinstance(pr, dict):
        lock_paths.append(pr_claim_lock_path(int(pr["number"])))
    held = [_lock_is_held(item) for item in lock_paths]
    local_held = bool(held) and all(held)
    if held and any(value != held[0] for value in held[1:]):
        state = "ambiguous"
    elif local_held and record.get("released_at") is not None:
        state = "ambiguous"
    elif local_held:
        state = "live"
    elif record.get("released_at") is None:
        state = "stale"
    else:
        state = "released"
    return {
        "state": state, "task_gid": task_gid, "branch": record["branch"],
        "lineage_id": record.get("lineage_id"), "agent_id": record["agent_id"],
        "host": record["host"], "acquired_at": record["acquired_at"],
        "released_at": record.get("released_at"), "takeover": bool(record.get("takeover")),
        "claim_id": record["token"], "pr": record.get("pr"),
        "head_moved_to": record.get("head_moved_to"),
        "semantic_mutation_closed_at": record.get("semantic_mutation_closed_at"),
        "claim_path": str(path),
    }


def claim_status(task_gid: str) -> dict[str, Any]:
    task_gid = require_task_gid(task_gid)
    if os.environ.get(CLAIM_TASK_ENV) == task_gid and os.environ.get(LINEAGE_ENV):
        try:
            record = read_claim(task_gid)
        except Exception as exc:
            if hasattr(exc, "code"):
                return {"state": "ambiguous", "diagnostic": f"{exc.code}: {exc.message}"}
            raise
        if record is None:
            return {"state": "missing", "task_gid": task_gid}
        return _claim_status_record(task_gid, record, claim_path(task_gid))
    paths = claim_paths(task_gid)
    if not paths:
        return {"state": "missing", "task_gid": task_gid}
    statuses=[]
    for path in paths:
        record=_validate_claim_record(read_json_object(path, "local-agent ownership claim"), task_gid)
        statuses.append(_claim_status_record(task_gid, record, path))
    if len(statuses)==1:
        return statuses[0]
    return {"state":"multiple", "task_gid":task_gid, "lineages":statuses}


def normalize_pr_identity(
    number: int | None,
    head: str | None,
    lease_state: str | None,
    lease_id: str | None,
    *,
    required: bool = False,
) -> dict[str, Any] | None:
    supplied = [number is not None, head is not None, lease_state is not None, lease_id is not None]
    if not any(supplied):
        if required:
            fail(
                "PR_LEASE_VISIBILITY_REQUIRED",
                "replacement PR identity requires --pr-number, --pr-head, and --pr-lease-state",
            )
        return None
    if number is None or head is None or lease_state is None:
        fail(
            "PR_LEASE_VISIBILITY_REQUIRED",
            "PR dispatch requires --pr-number, --pr-head, and --pr-lease-state so local ownership is reconciled with PR lease visibility",
        )
    if number <= 0:
        fail("INVALID_PR", "PR number must be a positive integer")
    head = require_full_sha(head, "PR head SHA")
    if lease_state == "active" and not lease_id:
        fail("PR_LEASE_VISIBILITY_REQUIRED", "active PR lease visibility requires --pr-lease-id")
    if lease_state == "none" and lease_id is not None:
        fail("PR_LEASE_VISIBILITY_AMBIGUOUS", "--pr-lease-id cannot be supplied when --pr-lease-state=none")
    return {"number": number, "head": head, "lease_state": lease_state, "lease_id": lease_id}


def _normalize_pr(args: argparse.Namespace) -> dict[str, Any] | None:
    return normalize_pr_identity(
        args.pr_number, args.pr_head, args.pr_lease_state, args.pr_lease_id
    )


def lineage_claim_locks(
    task_gid: str,
    branches: list[str],
    pr_numbers: list[int] | None = None,
) -> "_HeldClaimLocks":
    paths = [branch_claim_lock_path(branch) for branch in branches]
    paths.extend(pr_claim_lock_path(number) for number in (pr_numbers or []))
    return _HeldClaimLocks(paths)


def remove_archived_claim(
    task_gid: str,
    archived_claim: dict[str, Any] | None,
    expected_branch: str,
) -> None:
    path = claim_path(task_gid)
    if not path.exists():
        return
    current = read_claim(task_gid)
    if current is None:
        return
    if archived_claim is None:
        fail("SUPERSESSION_CLAIM_CHANGED", "a task ownership claim appeared after supersession terminalization")
    if (
        current.get("token") != archived_claim.get("token")
        or current.get("branch") != expected_branch
        or current.get("agent_id") != archived_claim.get("agent_id")
    ):
        fail("SUPERSESSION_CLAIM_CHANGED", "task ownership claim changed from the archived old-lineage claim")
    path.unlink()
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _remove_claim_file(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _coherent_prior_claim(
    previous: dict[str, Any],
    state: dict[str, Any],
    state_owner: str,
) -> dict[str, Any]:
    """Repair only stale claim bookkeeping to the already-authoritative task owner."""
    repaired = dict(previous)
    repaired["agent_id"] = state_owner
    owner = state.get("owner")
    if isinstance(owner, dict) and owner.get("host") is not None:
        repaired["host"] = str(owner["host"])
    repaired["released_at"] = repaired.get("released_at") or now_utc()
    repaired["takeover"] = False
    return repaired


def _reconcile_existing(
    *,
    runner: GitRunner,
    task_gid: str,
    branch: str,
    lineage_id: str,
    agent_id: str,
    takeover: bool,
    expected_claim: str | None,
    pr: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    exact_claim = claim_path(task_gid, branch, lineage_id)
    previous = read_claim(task_gid, branch, lineage_id) if exact_claim.exists() else None
    legacy_claim = claim_root() / f"{task_gid}.json"
    if previous is None and legacy_claim.exists():
        candidate = _validate_claim_record(read_json_object(legacy_claim, "legacy local-agent ownership claim"), task_gid)
        if candidate.get("branch") != branch:
            fail("OWNERSHIP_AMBIGUOUS", f"legacy claim names branch {candidate.get('branch')!r}, not requested {branch!r}")
        candidate = dict(candidate)
        candidate["schema_version"] = CLAIM_SCHEMA_VERSION
        candidate["lineage_id"] = lineage_id
        atomic_write_json(exact_claim, candidate)
        _remove_claim_file(legacy_claim)
        previous = candidate

    state_file = state_path_for_branch(task_gid, branch)
    state = None
    if state_file is not None:
        state = read_json_object(state_file, "task worktree state")
        if state.get("lifecycle") != "active":
            fail("TASK_NOT_ACTIVE", f"lineage lifecycle is {state.get('lifecycle')!r}; a new live ownership claim is refused")
        existing_lineage = state.get("lineage_id")
        if existing_lineage is None:
            state = dict(state)
            state["lineage_id"] = lineage_id
            exact_state = state_path(task_gid, branch, lineage_id)
            atomic_write_json(exact_state, state)
            if state_file != exact_state:
                state_file.unlink()
            state_file = exact_state
        elif existing_lineage != lineage_id:
            fail("LINEAGE_REGISTRY_CHANGED", f"local state lineage {existing_lineage} != repository lineage {lineage_id}")
        state_repo = resolve_repository_from_state(runner, state)
        verify_owned_worktree(runner, state_repo, state)
        stored_pr = state.get("pr_url") or state.get("pr_head")
        if stored_pr and pr is None:
            fail("PR_LEASE_VISIBILITY_REQUIRED", "lineage state already carries PR identity; dispatch must provide current PR head/lease visibility")

    if previous is not None:
        previous_pr = previous.get("pr")
        if previous_pr is not None and pr is None:
            fail("PR_LEASE_VISIBILITY_REQUIRED", "prior ownership claim carries PR identity; dispatch must reconcile current PR head/lease visibility")
        if isinstance(previous_pr, dict) and isinstance(pr, dict) and previous_pr.get("number") != pr.get("number"):
            fail("OWNERSHIP_AMBIGUOUS", f"prior claim names PR #{previous_pr.get('number')}, but dispatch requested PR #{pr.get('number')}")

    state_owner = owner_agent_id(state) if state is not None else None
    prior_owner = previous.get("agent_id") if previous is not None else None
    if state_owner is not None and prior_owner is not None and state_owner != prior_owner:
        exact_takeover = takeover and previous is not None and previous.get("token") == expected_claim
        same_durable_owner = not takeover and agent_id == state_owner
        if not (exact_takeover or same_durable_owner):
            token = str(previous.get("token"))
            fail(
                "OWNERSHIP_AMBIGUOUS",
                f"lineage owner {state_owner!r} disagrees with prior claim owner {prior_owner!r}. "
                f"Durable owner may reacquire ordinarily; replacement requires `claim --takeover --expected-claim {token}`.",
            )
        previous = _coherent_prior_claim(previous, state, state_owner)
        prior_owner = state_owner
    durable_owner = state_owner or prior_owner
    if durable_owner is None and state is not None and not takeover:
        fail("OWNER_HANDOFF_REQUIRED", "existing active lineage has no concrete owner; explicit --takeover is required")
    if durable_owner is not None and durable_owner != agent_id and not takeover:
        fail("OWNER_HANDOFF_REQUIRED", f"lineage belongs to prior local owner {durable_owner!r}; explicit handoff requires claim --takeover")
    return previous, state_owner


def _settle_failed_claim(
    *,
    task_gid: str,
    branch: str,
    lineage_id: str,
    path: Path,
    token: str,
    agent_id: str,
    previous: dict[str, Any] | None,
    baseline_state_owner: str | None,
) -> str | None:
    """Leave claim bookkeeping coherent with whichever owner the child durably persisted.

    Returns the resolved durable owner agent id (or ``None`` if there was never a
    prior owner), so the caller can roll the repository-wide registry marker's
    ``owner_agent_id`` back into agreement with this local settlement.
    """
    current = read_claim(task_gid, branch, lineage_id)
    if current is None or current.get("token") != token:
        fail("OWNERSHIP_AMBIGUOUS", "claim metadata changed while this agent still held the live ownership locks")
    state_file = state_path_for_branch(task_gid, branch)
    state = read_json_object(state_file, "task worktree state") if state_file is not None else None
    post_owner = owner_agent_id(state) if state is not None else None
    rollback_owner = previous.get("agent_id") if previous is not None else baseline_state_owner
    if post_owner == agent_id:
        current["released_at"] = now_utc()
        atomic_write_json(path, current)
        return agent_id
    if state is None or post_owner == rollback_owner:
        if previous is None:
            _remove_claim_file(path)
        else:
            atomic_write_json(path, previous)
        return rollback_owner
    assert state is not None and post_owner is not None
    current["agent_id"] = post_owner
    owner = state.get("owner")
    if isinstance(owner, dict) and owner.get("host") is not None:
        current["host"] = str(owner["host"])
    current["released_at"] = now_utc()
    current["takeover"] = False
    atomic_write_json(path, current)
    return post_owner


class _HeldClaimLocks:
    def __init__(self, paths: list[Path]):
        self.paths = sorted(set(paths), key=str)
        self.fds: list[int] = []

    def acquire(self) -> None:
        for path in self.paths:
            fd = _open_lock(path)
            if not _try_lock(fd):
                os.close(fd)
                for held in reversed(self.fds):
                    _unlock_close(held)
                self.fds = []
                fail(
                    "OWNERSHIP_CLAIMED",
                    "another live local agent already holds the task/branch/PR ownership claim; stop and resume/handoff to that owner",
                )
            self.fds.append(fd)

    def release(self) -> None:
        for fd in reversed(self.fds):
            _unlock_close(fd)
        self.fds = []


def command_claim(args: argparse.Namespace, runner: GitRunner) -> int:
    task_gid = require_task_gid(args.task)
    agent_id = require_agent_id(args.agent_id)
    if agent_id is None:
        fail("OWNERSHIP_AGENT_REQUIRED", "exclusive local-agent ownership requires --agent-id")
    if args.require_launch_provenance and not args.launch_provenance:
        fail("LAUNCH_PROVENANCE_REQUIRED", "--require-launch-provenance requires --launch-provenance from the actual local host launcher")
    if not args.launch_provenance:
        require_repository_mutation_identity(agent_id, task_gid)
    repo = discover_repository(runner, Path(args.repo))
    branch = validate_branch(runner, repo.source_top, args.branch)
    pr = _normalize_pr(args)
    expected_claim = args.expected_claim
    if args.takeover and not expected_claim:
        fail("EXPECTED_CLAIM_REQUIRED", "claim --takeover requires --expected-claim with the exact current claim id")
    if expected_claim and not args.takeover:
        fail("EXPECTED_CLAIM_REQUIRES_TAKEOVER", "--expected-claim is valid only with claim --takeover")
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        fail("CLAIM_COMMAND_REQUIRED", "claim must wrap the dispatched local-agent command after --")

    token = uuid.uuid4().hex
    resolved_sha, lineage_id, resolved_marker = resolve_lineage_for_branch(
        runner, repo, task_gid=task_gid, branch=branch,
    )
    lock_paths = [branch_claim_lock_path(branch)]
    if pr is not None:
        lock_paths.append(pr_claim_lock_path(int(pr["number"])))
    locks = _HeldClaimLocks(lock_paths)
    locks.acquire()
    path = claim_path(task_gid, branch, lineage_id)
    prior_identity: dict[str, Any] | None = None
    launch_identity_bound = False
    registry_claimed = False
    pr_registry_acquired = False
    prior_registry_owner: str | None = None
    try:
        # Run the local claim-vs-state consistency check before the repository-wide
        # registry ownership gate, so a locally detectable OWNERSHIP_AMBIGUOUS
        # surfaces ahead of a registry-level OWNER_HANDOFF_REQUIRED.
        previous, baseline_state_owner = _reconcile_existing(
            runner=runner, task_gid=task_gid, branch=branch, lineage_id=lineage_id,
            agent_id=agent_id, takeover=bool(args.takeover), expected_claim=expected_claim, pr=pr,
        )
        if args.takeover and previous is not None and previous.get("token") != expected_claim:
            fail("OWNER_CLAIM_CHANGED", "takeover expected claim does not match the current durable local claim generation")
        registry_sha, registry_generation, prior_registry_owner = claim_registry_ownership(
            runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id,
            sha=resolved_sha, marker=resolved_marker, agent_id=agent_id, token=token,
            takeover=bool(args.takeover), expected_claim=expected_claim,
        )
        registry_claimed = True
        if pr is not None:
            acquire_pr_registry_claim(
                runner, repo, pr_number=int(pr["number"]), task_gid=task_gid, branch=branch,
                lineage_id=lineage_id, token=token, agent_id=agent_id,
            )
            pr_registry_acquired = True
        if pr is not None:
            remote_head = remote_ref_sha(runner, repo, f"refs/heads/{branch}")
            if remote_head != pr["head"]:
                fail("PR_BRANCH_HEAD_MISMATCH", f"authorized PR head {pr['head']} does not equal remote branch {branch} head {remote_head}")
        if args.launch_provenance:
            prior_identity, _ = bind_identity_from_launch_provenance(
                Path(args.launch_provenance), agent_id=agent_id, task_gid=task_gid, branch=branch, repo_path=repo.source_top, pr=pr,
            )
            launch_identity_bound = True
            require_repository_mutation_identity(agent_id, task_gid)
        else:
            require_repository_mutation_identity(agent_id, task_gid)
        record: dict[str, Any] = {
            "schema_version": CLAIM_SCHEMA_VERSION, "repository": EXPECTED_REPOSITORY, "origin_id": repo.origin_id,
            "task_gid": task_gid, "branch": branch, "lineage_id": lineage_id, "agent_id": agent_id,
            "host": socket.gethostname(), "token": token, "registry_marker": registry_sha,
            "registry_generation": registry_generation, "acquired_at": now_utc(), "released_at": None,
            "takeover": bool(args.takeover), "pr": pr, "launch_provenance_required": bool(args.require_launch_provenance),
        }
        atomic_write_json(path, record)
        env = os.environ.copy()
        env.update({CLAIM_TOKEN_ENV: token, CLAIM_TASK_ENV: task_gid, CLAIM_BRANCH_ENV: branch, CLAIM_AGENT_ENV: agent_id, LINEAGE_ENV: lineage_id})
        try:
            completed = subprocess.run(argv, cwd=repo.source_top, env=env, check=False, pass_fds=tuple(locks.fds))
        except OSError as exc:
            resolved_owner = _settle_failed_claim(task_gid=task_gid, branch=branch, lineage_id=lineage_id, path=path, token=token, agent_id=agent_id, previous=previous, baseline_state_owner=baseline_state_owner)
            if launch_identity_bound:
                restore_identity(agent_id, prior_identity)
            if pr_registry_acquired:
                release_pr_registry_claim(runner, repo, pr_number=int(pr["number"]), lineage_id=lineage_id, token=token)
            release_registry_claim(runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id, token=token, owner_agent_id=resolved_owner)
            fail("CLAIM_COMMAND_FAILED", f"could not launch claimed local-agent command: {exc}")
        if completed.returncode != 0:
            resolved_owner = _settle_failed_claim(task_gid=task_gid, branch=branch, lineage_id=lineage_id, path=path, token=token, agent_id=agent_id, previous=previous, baseline_state_owner=baseline_state_owner)
            if launch_identity_bound:
                state_file = state_path_for_branch(task_gid, branch)
                durable_state = read_json_object(state_file, "task worktree state") if state_file is not None else None
                durable_owner = owner_agent_id(durable_state) if durable_state is not None else None
                if durable_owner != agent_id:
                    restore_identity(agent_id, prior_identity)
            if pr_registry_acquired:
                release_pr_registry_claim(runner, repo, pr_number=int(pr["number"]), lineage_id=lineage_id, token=token)
            release_registry_claim(runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id, token=token, owner_agent_id=resolved_owner)
            return completed.returncode
        current = read_claim(task_gid, branch, lineage_id)
        if current is None or current.get("token") != token:
            fail("OWNERSHIP_AMBIGUOUS", "claim metadata changed while this agent still held the live ownership locks")
        current["released_at"] = now_utc()
        atomic_write_json(path, current)
        if pr_registry_acquired:
            release_pr_registry_claim(runner, repo, pr_number=int(pr["number"]), lineage_id=lineage_id, token=token)
        release_registry_claim(runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id, token=token)
        return completed.returncode
    except Exception:
        # If setup fails after the repository-wide claim was acquired, release it
        # when it is still ours. A release failure is intentionally fail-closed.
        try:
            if pr_registry_acquired:
                try:
                    release_pr_registry_claim(runner, repo, pr_number=int(pr["number"]), lineage_id=lineage_id, token=token)
                except Exception:
                    pass
            if registry_claimed:
                reg = read_registry(runner, repo, branch)
                if reg is not None:
                    _, marker = reg
                    if marker.get("lineage_id") == lineage_id and marker.get("claim_token") == token and marker.get("claim_active"):
                        release_registry_claim(runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id, token=token, owner_agent_id=prior_registry_owner)
        finally:
            if launch_identity_bound:
                restore_identity(agent_id, prior_identity)
        raise
    finally:
        locks.release()


def invalidate_claim_after_head_movement(task_gid: str, new_head: str) -> bool:
    task_gid = require_task_gid(task_gid)
    new_head = require_full_sha(new_head, "new PR head")
    token = os.environ.get(CLAIM_TOKEN_ENV)
    if not token:
        fail("OWNERSHIP_CLAIM_REQUIRED", "head-movement invalidation requires the live ownership claim")
    record = read_claim(task_gid)
    if record is None or record.get("token") != token or record.get("released_at") is not None:
        fail("OWNERSHIP_CLAIM_MISMATCH", "cannot invalidate a missing/stale/mismatched ownership claim")
    pr = record.get("pr")
    if not isinstance(pr, dict):
        return False
    if pr.get("head") == new_head:
        return False
    record["head_moved_to"] = new_head
    record["semantic_mutation_closed_at"] = now_utc()
    atomic_write_json(claim_path(task_gid), record)
    return True


def require_active_claim(
    task_gid: str,
    branch: str,
    agent_id: str | None,
    runner: GitRunner,
    *,
    allow_takeover: bool = False,
    require_pr: bool = False,
    allow_head_moved_readback: bool = False,
) -> dict[str, Any]:
    task_gid = require_task_gid(task_gid)
    agent_id = require_agent_id(agent_id)
    if agent_id is None:
        fail("OWNERSHIP_AGENT_REQUIRED", "local-agent writer operations require --agent-id inside an active ownership claim")
    require_repository_mutation_identity(agent_id, task_gid)
    token = os.environ.get(CLAIM_TOKEN_ENV)
    lineage_id = os.environ.get(LINEAGE_ENV)
    if not token or not lineage_id or os.environ.get(CLAIM_TASK_ENV) != task_gid or os.environ.get(CLAIM_BRANCH_ENV) != branch:
        fail("OWNERSHIP_CLAIM_REQUIRED", "local-agent writer operations require the exact task/branch/lineage claim environment")
    record = read_claim(task_gid, branch, lineage_id)
    if record is None:
        fail("OWNERSHIP_CLAIM_REQUIRED", "no durable local-agent claim metadata exists for this exact lineage")
    if record.get("token") != token or record.get("lineage_id") != lineage_id:
        fail("OWNERSHIP_CLAIM_MISMATCH", "claim token/lineage does not match the durable ownership claim")
    if record.get("branch") != branch or record.get("agent_id") != agent_id:
        fail("OWNERSHIP_CLAIM_MISMATCH", "claim task/branch/agent identity does not match the requested writer operation")
    if record.get("released_at") is not None:
        fail("OWNERSHIP_CLAIM_STALE", "ownership claim was already released")
    if record.get("head_moved_to") is not None and not allow_head_moved_readback:
        fail("PR_HEAD_MOVED_REDISPATCH_REQUIRED", f"owned PR head moved to {record.get('head_moved_to')}; reacquire an exact-head claim")
    status = claim_status(task_gid)
    if status.get("state") != "live":
        fail("OWNERSHIP_CLAIM_STALE", f"ownership claim is {status.get('state')!r}, not live")
    state_file = state_path_for_branch(task_gid, branch)
    if state_file is not None:
        state = read_json_object(state_file, "task worktree state")
        if state.get("lineage_id") not in {None, lineage_id}:
            fail("LINEAGE_REGISTRY_CHANGED", "local state lineage differs from claimed lineage")
        repo = resolve_repository_from_state(runner, state) if state.get("lifecycle") == "active" else discover_repository(runner, Path("."))
    else:
        repo = discover_repository(runner, Path("."))
    marker = assert_active_registry_claim(runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id, token=token)
    if state_file is not None and state.get("published_head") is not None:
        expected_remote = str(state.get("published_head"))
        observed_remote = remote_ref_sha(runner, repo, f"refs/heads/{branch}", allow_missing=True)
        # A moved-but-present remote head (fast-forward, behind, or diverged) is
        # ordinary remote drift that resume's own remote-relation classification
        # already fails closed on (REMOTE_AHEAD/REMOTE_DIVERGED) without touching
        # the worktree or the registry. Only an outright missing branch is not
        # something that classification detects, so fence just that case here.
        if observed_remote is None:
            fence_lineage(
                runner, repo, task_gid=task_gid, branch=branch, lineage_id=lineage_id, token=token,
                reason="published branch disappeared or moved outside the admitted lineage",
                expected_head=expected_remote, observed_head=observed_remote,
            )
            fail("LINEAGE_REMOTE_CONTRADICTION", f"published branch {branch!r} expected {expected_remote}, observed <missing>; lineage permanently fenced")
    if require_pr and record.get("pr") is None:
        fail("PR_LEASE_VISIBILITY_REQUIRED", "adopting a handed-off PR branch requires current PR head/lease visibility")
    if allow_takeover and not record.get("takeover"):
        fail("TAKEOVER_CLAIM_REQUIRED", "resume --takeover requires the live dispatch claim to have been acquired with --takeover")
    if state_file is not None:
        current_owner = owner_agent_id(state)
        if current_owner != agent_id:
            if not allow_takeover:
                fail("OWNER_MISMATCH", f"lineage owner provenance is {current_owner!r}; this claim does not authorize silent replacement")
            if not record.get("takeover"):
                fail("TAKEOVER_CLAIM_REQUIRED", "ownership replacement requires both claim --takeover and resume --takeover")
    return record
