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
from .repository import discover_repository
from . import global_claim
from .state import (
    atomic_write_json,
    load_task_state,
    read_json_object,
    state_path,
    state_root,
    validate_agent_state,
)

CLAIM_SCHEMA_VERSION = 2
CLAIM_TOKEN_ENV = "DISH_AGENT_CLAIM_TOKEN"
CLAIM_TASK_ENV = "DISH_AGENT_CLAIM_TASK"
CLAIM_BRANCH_ENV = "DISH_AGENT_CLAIM_BRANCH"
CLAIM_AGENT_ENV = "DISH_AGENT_CLAIM_AGENT"
GLOBAL_CLAIM_ENV = global_claim.GLOBAL_CLAIM_ENV
GLOBAL_WRITER_ENV = global_claim.GLOBAL_WRITER_ENV


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


def claim_path(task_gid: str) -> Path:
    return claim_root() / f"{require_task_gid(task_gid)}.json"


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
    schema_version = record.get("schema_version")
    if schema_version not in {1, CLAIM_SCHEMA_VERSION}:
        fail("OWNERSHIP_AMBIGUOUS", f"unsupported claim schema {schema_version!r}")
    record.setdefault("global_claim_id", None)
    record.setdefault("global_writer_capability", None)
    capability = record.get("global_writer_capability")
    if schema_version == CLAIM_SCHEMA_VERSION and (not isinstance(capability, str) or len(capability) < 32):
        fail("OWNERSHIP_AMBIGUOUS", "claim record is missing the private global writer capability")
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


def read_claim(task_gid: str) -> dict[str, Any] | None:
    path = claim_path(task_gid)
    if not path.exists():
        return None
    return _validate_claim_record(read_json_object(path, "local-agent ownership claim"), task_gid)


def claim_status(task_gid: str) -> dict[str, Any]:
    task_gid = require_task_gid(task_gid)
    try:
        record = read_claim(task_gid)
    except Exception as exc:
        if hasattr(exc, "code"):
            return {"state": "ambiguous", "diagnostic": f"{exc.code}: {exc.message}"}
        raise
    if record is None:
        return {"state": "missing", "task_gid": task_gid}
    lock_paths = [task_claim_lock_path(task_gid), branch_claim_lock_path(str(record["branch"]))]
    pr = record.get("pr")
    if isinstance(pr, dict):
        lock_paths.append(pr_claim_lock_path(int(pr["number"])))
    held = [_lock_is_held(path) for path in lock_paths]
    if any(value != held[0] for value in held[1:]):
        state = "ambiguous"
    elif held[0] and record.get("released_at") is not None:
        state = "ambiguous"
    elif held[0]:
        state = "live"
    elif record.get("released_at") is None:
        state = "stale"
    else:
        state = "released"
    return {
        "state": state,
        "task_gid": task_gid,
        "branch": record["branch"],
        "agent_id": record["agent_id"],
        "host": record["host"],
        "acquired_at": record["acquired_at"],
        "released_at": record.get("released_at"),
        "takeover": bool(record.get("takeover")),
        "claim_id": record["token"],
        "global_claim_id": record.get("global_claim_id"),
        "pr": record.get("pr"),
    }


def _normalize_pr(args: argparse.Namespace) -> dict[str, Any] | None:
    number = args.pr_number
    head = args.pr_head
    lease_state = args.pr_lease_state
    lease_id = args.pr_lease_id
    supplied = [number is not None, head is not None, lease_state is not None, lease_id is not None]
    if not any(supplied):
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


def _reconcile_existing(
    *,
    runner: GitRunner,
    task_gid: str,
    branch: str,
    agent_id: str,
    takeover: bool,
    pr: dict[str, Any] | None,
) -> dict[str, Any] | None:
    previous = read_claim(task_gid)
    state = load_task_state(task_gid) if state_path(task_gid).exists() else None

    if state is not None:
        if state.get("lifecycle") != "active":
            fail("TASK_NOT_ACTIVE", f"task lifecycle is {state.get('lifecycle')!r}; a new live ownership claim is refused")
        state_repo = resolve_repository_from_state(runner, state)
        verify_owned_worktree(runner, state_repo, state)
        if state.get("branch") != branch:
            fail(
                "OWNERSHIP_AMBIGUOUS",
                f"task worktree state owns branch {state.get('branch')!r}, but dispatch requested {branch!r}",
            )
        stored_pr = state.get("pr_url") or state.get("pr_head")
        if stored_pr and pr is None:
            fail(
                "PR_LEASE_VISIBILITY_REQUIRED",
                "task state already carries PR identity; dispatch must provide current PR head/lease visibility",
            )

    if previous is not None:
        if previous.get("branch") != branch:
            fail(
                "OWNERSHIP_AMBIGUOUS",
                f"stale/released claim names branch {previous.get('branch')!r}, but dispatch requested {branch!r}",
            )
        previous_pr = previous.get("pr")
        if previous_pr is not None and pr is None:
            fail(
                "PR_LEASE_VISIBILITY_REQUIRED",
                "prior ownership claim carries PR identity; dispatch must reconcile current PR head/lease visibility",
            )
        if isinstance(previous_pr, dict) and isinstance(pr, dict) and previous_pr.get("number") != pr.get("number"):
            fail(
                "OWNERSHIP_AMBIGUOUS",
                f"prior claim names PR #{previous_pr.get('number')}, but dispatch requested PR #{pr.get('number')}",
            )

    state_owner = owner_agent_id(state) if state is not None else None
    prior_owner = previous.get("agent_id") if previous is not None else None
    if state_owner is not None and prior_owner is not None and state_owner != prior_owner:
        fail(
            "OWNERSHIP_AMBIGUOUS",
            f"task worktree owner {state_owner!r} disagrees with prior claim owner {prior_owner!r}",
        )
    durable_owner = state_owner or prior_owner
    if durable_owner is None and state is not None and not takeover:
        fail(
            "OWNER_HANDOFF_REQUIRED",
            "existing active task state has no concrete owner; explicit --takeover is required before assigning a local writer",
        )
    if durable_owner is not None and durable_owner != agent_id and not takeover:
        fail(
            "OWNER_HANDOFF_REQUIRED",
            f"task/branch belongs to prior local owner {durable_owner!r}; explicit handoff requires claim --takeover",
        )
    return previous


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



def _option(argv: list[str], name: str) -> str | None:
    try:
        index = argv.index(name)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def _global_base(args: argparse.Namespace, task_gid: str, argv: list[str]) -> str:
    supplied = getattr(args, "base", None)
    if supplied:
        return require_full_sha(supplied, "global claim authoring base SHA")
    if state_path(task_gid).exists():
        state = load_task_state(task_gid)
        return require_full_sha(str(state.get("base_sha")), "stored authoring base SHA")
    child_base = _option(argv, "--base")
    if child_base:
        return require_full_sha(child_base, "child authoring base SHA")
    fail(
        "GLOBAL_CLAIM_BASE_REQUIRED",
        "global Implementation ownership requires the exact authoring base SHA via claim --base or the wrapped start/adopt command",
    )


def _obtain_global_claim(
    args: argparse.Namespace, *, task_gid: str, branch: str, agent_id: str, argv: list[str], pr: dict[str, Any] | None
) -> dict[str, Any]:
    base_sha = _global_base(args, task_gid, argv)
    prior = read_claim(task_gid)
    explicit = getattr(args, "global_claim_id", None)
    prior_global = prior.get("global_claim_id") if prior is not None else None
    prior_writer = prior.get("global_writer_capability") if prior is not None else None
    if args.takeover:
        expected = getattr(args, "expected_global_claim", None)
        if not expected:
            fail(
                "EXPECTED_GLOBAL_CLAIM_REQUIRED",
                "claim --takeover requires --expected-global-claim with the exact current durable global claim id",
            )
        reason = getattr(args, "takeover_reason", None)
        evidence = getattr(args, "liveness_evidence", None)
        if not reason or not evidence:
            fail(
                "GLOBAL_TAKEOVER_EVIDENCE_REQUIRED",
                "global takeover requires --takeover-reason and --liveness-evidence; time passage alone is not ownership transfer",
            )
        current = global_claim.takeover(
            task_gid=task_gid, expected_claim_id=expected, owner=agent_id,
            session_id=getattr(args, "session_id", None) or f"{agent_id}:{uuid.uuid4().hex}",
            host=socket.gethostname(), base_sha=base_sha, reason=reason, liveness_evidence=evidence,
        )
    elif explicit or prior_global:
        claim_id = str(explicit or prior_global)
        current = global_claim.authorize(task_gid, claim_id, branch, writer_capability=str(prior_writer or ""))
        if current.get("authoring_base_sha") != base_sha:
            fail(
                "GLOBAL_CLAIM_BASE_MISMATCH",
                f"current global claim authoring base {current.get('authoring_base_sha')} does not equal requested {base_sha}",
            )
    else:
        current = global_claim.acquire(
            task_gid=task_gid, owner=agent_id,
            session_id=getattr(args, "session_id", None) or f"{agent_id}:{uuid.uuid4().hex}",
            host=socket.gethostname(), base_sha=base_sha, branch=branch,
        )
    writer_capability = current.get("writer_capability") or prior_writer
    if not isinstance(writer_capability, str) or len(writer_capability) < 32:
        fail("WRITER_AUTHORITY_REQUIRED", "global claim did not return/preserve private writer authority")
    if current.get("branch") != branch:
        fail("GLOBAL_LINEAGE_CONFLICT", f"global claim is bound to branch {current.get('branch')!r}, not {branch!r}")
    if pr is not None:
        current = global_claim.bind_pr(
            task_gid, str(current["claim_id"]), int(pr["number"]), str(pr["head"]),
            writer_capability=writer_capability,
        )
    current = dict(current)
    current["writer_capability"] = writer_capability
    return current

def command_claim(args: argparse.Namespace, runner: GitRunner) -> int:
    task_gid = require_task_gid(args.task)
    agent_id = require_agent_id(args.agent_id)
    if agent_id is None:
        fail("OWNERSHIP_AGENT_REQUIRED", "exclusive local-agent ownership requires --agent-id")
    validate_agent_state(agent_id)

    repo = discover_repository(runner, Path(args.repo))
    branch = validate_branch(runner, repo.source_top, args.branch)
    pr = _normalize_pr(args)
    expected_claim = args.expected_claim
    if args.takeover and not expected_claim:
        fail(
            "EXPECTED_CLAIM_REQUIRED",
            "claim --takeover requires --expected-claim with the exact current claim id",
        )
    if expected_claim and not args.takeover:
        fail(
            "EXPECTED_CLAIM_REQUIRES_TAKEOVER",
            "--expected-claim is valid only with claim --takeover",
        )
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        fail("CLAIM_COMMAND_REQUIRED", "claim must wrap the dispatched local-agent command after --")

    # Cheap fail-closed preflight prevents a new global generation from being created
    # for an assignment that already contradicts durable same-host lineage. Full
    # reconciliation is repeated while the local locks are held below.
    preexisting = read_claim(task_gid)
    if preexisting is not None:
        if preexisting.get("branch") != branch:
            fail(
                "OWNERSHIP_AMBIGUOUS",
                f"stale/released claim names branch {preexisting.get('branch')!r}, but dispatch requested {branch!r}",
            )
        if not args.takeover and preexisting.get("agent_id") != agent_id:
            fail(
                "OWNER_HANDOFF_REQUIRED",
                f"task/branch belongs to prior local owner {preexisting.get('agent_id')!r}; explicit handoff requires claim --takeover",
            )

    lock_paths = [task_claim_lock_path(task_gid), branch_claim_lock_path(branch)]
    if pr is not None:
        lock_paths.append(pr_claim_lock_path(int(pr["number"])))
    locks = _HeldClaimLocks(lock_paths)
    locked = False
    previous = None
    try:
        if args.takeover:
            # A live same-host claimant is direct liveness evidence. Acquire the
            # local process fences before transferring the durable global generation.
            locks.acquire()
            locked = True
            previous = _reconcile_existing(
                runner=runner, task_gid=task_gid, branch=branch, agent_id=agent_id, takeover=True, pr=pr
            )
            if previous is None:
                if expected_claim != "legacy-unclaimed" or not state_path(task_gid).exists():
                    fail(
                        "OWNER_CLAIM_CHANGED",
                        "takeover expected claim does not match the current durable ownership generation",
                    )
            elif previous.get("token") != expected_claim:
                fail(
                    "OWNER_CLAIM_CHANGED",
                    "takeover expected claim does not match the current durable ownership generation",
                )
            global_record = _obtain_global_claim(
                args, task_gid=task_gid, branch=branch, agent_id=agent_id, argv=argv, pr=pr
            )
        else:
            global_record = _obtain_global_claim(
                args, task_gid=task_gid, branch=branch, agent_id=agent_id, argv=argv, pr=pr
            )
            locks.acquire()
            locked = True
            previous = _reconcile_existing(
                runner=runner, task_gid=task_gid, branch=branch, agent_id=agent_id, takeover=False, pr=pr
            )

        token = uuid.uuid4().hex
        path = claim_path(task_gid)
        if pr is not None:
            remote_head = remote_ref_sha(runner, repo, f"refs/heads/{branch}")
            if remote_head != pr["head"]:
                fail(
                    "PR_BRANCH_HEAD_MISMATCH",
                    f"authorized PR head {pr['head']} does not equal remote branch {branch} head {remote_head}; dispatch lineage is stale or contradictory",
                )
        record: dict[str, Any] = {
            "schema_version": CLAIM_SCHEMA_VERSION,
            "repository": EXPECTED_REPOSITORY,
            "origin_id": repo.origin_id,
            "task_gid": task_gid,
            "branch": branch,
            "agent_id": agent_id,
            "host": socket.gethostname(),
            "token": token,
            "acquired_at": now_utc(),
            "released_at": None,
            "takeover": bool(args.takeover),
            "global_claim_id": global_record["claim_id"],
            "global_writer_capability": global_record["writer_capability"],
            "pr": pr,
        }
        atomic_write_json(path, record)

        env = os.environ.copy()
        env.update(
            {
                CLAIM_TOKEN_ENV: token,
                CLAIM_TASK_ENV: task_gid,
                CLAIM_BRANCH_ENV: branch,
                CLAIM_AGENT_ENV: agent_id,
                GLOBAL_CLAIM_ENV: str(global_record["claim_id"]),
                GLOBAL_WRITER_ENV: str(global_record["writer_capability"]),
            }
        )
        try:
            completed = subprocess.run(
                argv,
                cwd=repo.source_top,
                env=env,
                check=False,
                pass_fds=tuple(locks.fds),
            )
        except OSError as exc:
            fail("CLAIM_COMMAND_FAILED", f"could not launch claimed local-agent command: {exc}")
        current = read_claim(task_gid)
        if current is None or current.get("token") != token:
            fail("OWNERSHIP_AMBIGUOUS", "claim metadata changed while this agent still held the live ownership locks")
        current["released_at"] = now_utc()
        atomic_write_json(path, current)
        return completed.returncode
    finally:
        if locked:
            locks.release()


def require_active_claim(
    task_gid: str,
    branch: str,
    agent_id: str | None,
    *,
    allow_takeover: bool = False,
    require_pr: bool = False,
) -> dict[str, Any]:
    task_gid = require_task_gid(task_gid)
    agent_id = require_agent_id(agent_id)
    if agent_id is None:
        fail("OWNERSHIP_AGENT_REQUIRED", "local-agent writer operations require --agent-id inside an active ownership claim")
    token = os.environ.get(CLAIM_TOKEN_ENV)
    if not token:
        fail(
            "OWNERSHIP_CLAIM_REQUIRED",
            "local-agent writer operations must run inside `tools/agent-worktree claim`; direct unclaimed branch/worktree access is refused",
        )
    record = read_claim(task_gid)
    if record is None:
        fail("OWNERSHIP_CLAIM_REQUIRED", "no durable local-agent claim metadata exists for this task")
    if record.get("token") != token:
        fail("OWNERSHIP_CLAIM_MISMATCH", "claim token does not match the durable task ownership claim")
    if record.get("branch") != branch or record.get("agent_id") != agent_id:
        fail(
            "OWNERSHIP_CLAIM_MISMATCH",
            "claim task/branch/agent identity does not match the requested writer operation",
        )
    if record.get("released_at") is not None:
        fail("OWNERSHIP_CLAIM_STALE", "ownership claim was already released")
    global_id = record.get("global_claim_id")
    if not isinstance(global_id, str) or not global_id:
        fail(
            "GLOBAL_CLAIM_REQUIRED",
            "local ownership record predates the durable global claim; reacquire through tools/agent-worktree claim before writing",
        )
    if os.environ.get(GLOBAL_CLAIM_ENV) != global_id:
        fail("GLOBAL_CLAIM_MISMATCH", "process global claim id does not match the durable local ownership record")
    global_writer = record.get("global_writer_capability")
    if not isinstance(global_writer, str) or os.environ.get(GLOBAL_WRITER_ENV) != global_writer:
        fail("WRITER_AUTHORITY_DENIED", "process private writer capability does not match the durable local ownership record")
    global_claim.authorize(task_gid, global_id, branch, writer_capability=global_writer)
    status = claim_status(task_gid)
    if status.get("state") != "live":
        fail(
            "OWNERSHIP_CLAIM_STALE",
            f"ownership claim is {status.get('state')!r}, not live; dispatch/resume must reacquire it before writing",
        )
    if require_pr and record.get("pr") is None:
        fail(
            "PR_LEASE_VISIBILITY_REQUIRED",
            "adopting a handed-off PR branch requires current PR head/lease visibility on the live claim",
        )
    if allow_takeover and not record.get("takeover"):
        fail("TAKEOVER_CLAIM_REQUIRED", "resume --takeover requires the live dispatch claim to have been acquired with --takeover")
    if state_path(task_gid).exists():
        state = load_task_state(task_gid)
        current_owner = owner_agent_id(state)
        if current_owner != agent_id:
            if not allow_takeover:
                fail(
                    "OWNER_MISMATCH",
                    f"task record owner provenance is {current_owner!r}; this live claim does not authorize silent ownership replacement",
                )
            if not record.get("takeover"):
                fail("TAKEOVER_CLAIM_REQUIRED", "ownership replacement requires both claim --takeover and resume --takeover")
    return record
