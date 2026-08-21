from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .common import GitRunner, Repository, fail, now_utc, require_full_sha, require_task_gid
from .operations import ensure_commit_object, remote_ref_sha, validate_branch

REGISTRY_SCHEMA = "dish-agent-worktree-lineage-v1"
REGISTRY_REF_PREFIX = "refs/heads/dish-agent-lineage-registry/"
PR_REGISTRY_REF_PREFIX = "refs/heads/dish-agent-pr-registry/"
LINEAGE_ENV = "DISH_AGENT_LINEAGE_ID"


def branch_digest(branch: str) -> str:
    return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:24]


def registry_ref(branch: str) -> str:
    return f"{REGISTRY_REF_PREFIX}{branch_digest(branch)}"


def _marker_commit(runner: GitRunner, repo: Repository, payload: dict[str, Any], parent: str | None = None) -> str:
    tree = runner.run(repo.source_top, "mktree", stdin="").stdout.strip()
    args = ["commit-tree", tree]
    if parent is not None:
        args += ["-p", require_full_sha(parent, "lineage registry parent")]
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    return require_full_sha(runner.run(repo.source_top, *args, stdin=message).stdout.strip(), "lineage registry marker")


def _read_marker_object(runner: GitRunner, repo: Repository, sha: str) -> dict[str, Any]:
    sha = require_full_sha(sha, "lineage registry marker")
    ensure_commit_object(runner, repo, sha)
    raw = runner.run(repo.source_top, "show", "-s", "--format=%B", sha).stdout.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail("LINEAGE_REGISTRY_CORRUPT", f"cannot decode lineage registry marker {sha}: {exc}")
    if not isinstance(payload, dict) or payload.get("schema") != REGISTRY_SCHEMA:
        fail("LINEAGE_REGISTRY_CORRUPT", f"remote lineage registry marker {sha} has invalid schema")
    return payload


def read_registry(runner: GitRunner, repo: Repository, branch: str) -> tuple[str, dict[str, Any]] | None:
    validate_branch(runner, repo.source_top, branch)
    ref = registry_ref(branch)
    sha = remote_ref_sha(runner, repo, ref, allow_missing=True)
    if sha is None:
        return None
    payload = _read_marker_object(runner, repo, sha)
    if payload.get("branch") != branch or payload.get("branch_digest") != branch_digest(branch):
        fail("LINEAGE_REGISTRY_CORRUPT", "lineage registry branch identity does not match its ref")
    lineage_id = payload.get("lineage_id")
    if not isinstance(lineage_id, str) or len(lineage_id) < 16:
        fail("LINEAGE_REGISTRY_CORRUPT", "lineage registry has invalid lineage_id")
    if payload.get("state") not in {"ACTIVE", "TERMINAL"}:
        fail("LINEAGE_REGISTRY_CORRUPT", "lineage registry state must be ACTIVE or TERMINAL")
    return sha, payload


def _push_marker_cas(
    runner: GitRunner,
    repo: Repository,
    *,
    branch: str,
    marker_sha: str,
    expected_sha: str | None,
) -> None:
    ref = registry_ref(branch)
    lease = f"--force-with-lease={ref}:{expected_sha or ''}"
    result = runner.run(repo.source_top, "push", lease, repo.origin_url, f"{marker_sha}:{ref}", check=False)
    if result.returncode != 0:
        observed = remote_ref_sha(runner, repo, ref, allow_missing=True)
        detail = result.stderr.strip() or result.stdout.strip() or "no command output"
        fail(
            "LINEAGE_REGISTRY_CAS_FAILED",
            f"repository-wide branch-lineage CAS failed for {branch!r}; expected registry {expected_sha or '<absent>'}, observed {observed or '<absent>'}: {detail}",
        )
    observed = remote_ref_sha(runner, repo, ref)
    if observed != marker_sha:
        fail("LINEAGE_REGISTRY_VERIFY_FAILED", f"remote lineage registry {observed} != intended marker {marker_sha}")


def reserve_or_recover_lineage(
    runner: GitRunner,
    repo: Repository,
    *,
    task_gid: str,
    branch: str,
) -> tuple[str, str, dict[str, Any]]:
    """Return (registry_sha, lineage_id, marker), creating one reservation if absent.

    An ACTIVE reservation may be recovered only by the same task. Different tasks
    cannot adopt the branch name, and TERMINAL names are permanently retired.
    """
    task_gid = require_task_gid(task_gid)
    branch = validate_branch(runner, repo.source_top, branch)
    existing = read_registry(runner, repo, branch)
    if existing is not None:
        sha, marker = existing
        if marker["state"] == "TERMINAL":
            fail("BRANCH_NAME_RETIRED", f"agent-worktree-managed branch name is permanently retired: {branch}")
        if marker.get("task_gid") != task_gid:
            fail(
                "BRANCH_LINEAGE_OWNED",
                f"branch {branch!r} is already admitted to task {marker.get('task_gid')!r} under lineage {marker.get('lineage_id')}",
            )
        return sha, str(marker["lineage_id"]), marker

    lineage_id = uuid.uuid4().hex
    stamp = now_utc()
    initial_remote_head = remote_ref_sha(runner, repo, f"refs/heads/{branch}", allow_missing=True)
    marker = {
        "schema": REGISTRY_SCHEMA,
        "state": "ACTIVE",
        "repository": "marcogallotta/ai-tools",
        "task_gid": task_gid,
        "branch": branch,
        "branch_digest": branch_digest(branch),
        "lineage_id": lineage_id,
        "created_at": stamp,
        "updated_at": stamp,
        "claim_generation": 0,
        "claim_token": None,
        "claim_agent_id": None,
        "claim_active": False,
        "branch_head": initial_remote_head,
    }
    marker_sha = _marker_commit(runner, repo, marker)
    try:
        _push_marker_cas(runner, repo, branch=branch, marker_sha=marker_sha, expected_sha=None)
    except Exception as exc:
        observed = read_registry(runner, repo, branch)
        if observed is not None:
            _, winner = observed
            if winner.get("state") == "TERMINAL":
                fail("BRANCH_NAME_RETIRED", f"agent-worktree-managed branch name is permanently retired: {branch}")
            fail(
                "BRANCH_ADMISSION_RACE",
                f"branch {branch!r} was admitted concurrently to lineage {winner.get('lineage_id')}; this attempt is not a writer",
            )
        raise exc
    return marker_sha, lineage_id, marker


def resolve_lineage_for_branch(
    runner: GitRunner,
    repo: Repository,
    *,
    task_gid: str,
    branch: str,
) -> tuple[str, str, dict[str, Any]]:
    """Admit/recover the branch-name lineage and fence it against remote-head drift.

    This is the read/admission half of claiming a branch: it does not take the
    repository-wide writer claim or check per-agent ownership, so callers can run
    local-state consistency checks against the resolved ``lineage_id`` before the
    owner gate below can short-circuit with a durable-owner error.
    """
    sha, lineage_id, marker = reserve_or_recover_lineage(
        runner, repo, task_gid=task_gid, branch=branch
    )
    bound_head = marker.get("branch_head")
    if bound_head is not None:
        observed_head = remote_ref_sha(runner, repo, f"refs/heads/{branch}", allow_missing=True)
        if observed_head is not None and observed_head != bound_head:
            ensure_commit_object(runner, repo, bound_head)
            ensure_commit_object(runner, repo, observed_head)
            ff_check = runner.run(
                repo.source_top, "merge-base", "--is-ancestor", bound_head, observed_head, check=False,
            )
            if ff_check.returncode == 0:
                # Ordinary linear advancement (e.g. another publish/push moved the
                # owned branch forward without rewriting history): not an identity
                # contradiction, just remote drift. Leave the marker's branch_head as
                # last-recorded-by-publish and let the caller's own local-vs-remote
                # relation check (REMOTE_AHEAD/REMOTE_DIVERGED) handle it.
                return sha, lineage_id, marker
        if observed_head != bound_head:
            fenced = dict(marker)
            fenced.update(state="TERMINAL", disposition="identity-corruption", terminal_head=bound_head, observed_head=observed_head, terminalized_at=now_utc(), updated_at=now_utc(), claim_active=False)
            fenced_sha = _marker_commit(runner, repo, fenced, parent=sha)
            _push_marker_cas(runner, repo, branch=branch, marker_sha=fenced_sha, expected_sha=sha)
            fail("LINEAGE_REMOTE_CONTRADICTION", f"branch {branch!r} no longer matches admitted lineage head {bound_head}; observed {observed_head or '<missing>'}; lineage permanently fenced")
    return sha, lineage_id, marker


def claim_registry_ownership(
    runner: GitRunner,
    repo: Repository,
    *,
    task_gid: str,
    branch: str,
    lineage_id: str,
    sha: str,
    marker: dict[str, Any],
    agent_id: str,
    token: str,
    takeover: bool,
    expected_claim: str | None,
) -> tuple[str, int, str | None]:
    """Take the repository-wide writer claim for an already-resolved lineage marker."""
    durable_owner = marker.get("owner_agent_id")
    if durable_owner is not None and durable_owner != agent_id and not takeover:
        fail("OWNER_HANDOFF_REQUIRED", f"branch lineage belongs to repository-wide owner {durable_owner!r}; explicit takeover is required")
    if durable_owner is not None and durable_owner != agent_id and takeover:
        latest = str(marker.get("claim_token") or "")
        if expected_claim != latest:
            fail("OWNER_CLAIM_CHANGED", f"takeover expected claim does not match latest repository-wide claim {latest}")
    if marker.get("claim_active"):
        current = str(marker.get("claim_token") or "")
        if not takeover:
            fail(
                "OWNERSHIP_CLAIMED",
                f"branch lineage {branch!r}/{lineage_id} already has an active repository-wide writer claim {current}",
            )
        if not expected_claim or expected_claim != current:
            fail(
                "OWNER_CLAIM_CHANGED",
                f"takeover expected claim does not match the active repository-wide claim {current}",
            )
    elif takeover and expected_claim not in {None, "legacy-unclaimed"}:
        prior = marker.get("claim_token")
        if prior is None:
            fail(
                "OWNERSHIP_AMBIGUOUS",
                f"claim --takeover for {branch!r} used --expected-claim {expected_claim!r}, but lineage {lineage_id} "
                "has never been claimed; use --expected-claim legacy-unclaimed for a first claim on this branch",
            )
        if expected_claim != prior:
            fail("OWNER_CLAIM_CHANGED", "takeover expected claim does not match the latest repository-wide claim generation")

    generation = int(marker.get("claim_generation", 0)) + 1
    updated = dict(marker)
    updated.update(
        claim_generation=generation,
        claim_token=token,
        claim_agent_id=agent_id,
        owner_agent_id=agent_id,
        claim_active=True,
        updated_at=now_utc(),
    )
    new_sha = _marker_commit(runner, repo, updated, parent=sha)
    _push_marker_cas(runner, repo, branch=branch, marker_sha=new_sha, expected_sha=sha)
    return new_sha, generation, durable_owner


_NO_OWNER_OVERRIDE = object()


def release_registry_claim(
    runner: GitRunner,
    repo: Repository,
    *,
    task_gid: str,
    branch: str,
    lineage_id: str,
    token: str,
    owner_agent_id: Any = _NO_OWNER_OVERRIDE,
) -> None:
    """Release the active repository-wide writer claim.

    By default the marker's durable ``owner_agent_id`` is left as the agent that
    just held the claim. Pass ``owner_agent_id`` explicitly (a string, or ``None``
    for "no prior owner") to roll the durable owner back to its pre-claim value
    when the claimed local-agent command failed and never durably completed a
    handoff.
    """
    current = read_registry(runner, repo, branch)
    if current is None:
        fail("LINEAGE_REGISTRY_MISSING", f"repository-wide lineage registry disappeared for {branch}")
    sha, marker = current
    if marker.get("state") == "TERMINAL" and marker.get("task_gid") == task_gid and marker.get("lineage_id") == lineage_id and marker.get("claim_token") == token:
        return
    if marker.get("state") != "ACTIVE" or marker.get("task_gid") != task_gid or marker.get("lineage_id") != lineage_id:
        fail("LINEAGE_REGISTRY_CHANGED", "repository-wide lineage identity changed before claim release")
    if marker.get("claim_token") != token or not marker.get("claim_active"):
        fail("OWNERSHIP_CLAIM_MISMATCH", "repository-wide claim generation changed before release")
    updated = dict(marker)
    updated.update(claim_active=False, updated_at=now_utc())
    if owner_agent_id is not _NO_OWNER_OVERRIDE:
        updated["owner_agent_id"] = owner_agent_id
    new_sha = _marker_commit(runner, repo, updated, parent=sha)
    _push_marker_cas(runner, repo, branch=branch, marker_sha=new_sha, expected_sha=sha)


def assert_active_registry_claim(
    runner: GitRunner,
    repo: Repository,
    *,
    task_gid: str,
    branch: str,
    lineage_id: str,
    token: str,
) -> dict[str, Any]:
    current = read_registry(runner, repo, branch)
    if current is None:
        fail("LINEAGE_REGISTRY_MISSING", f"repository-wide lineage registry disappeared for {branch}")
    _, marker = current
    if marker.get("state") != "ACTIVE":
        fail("LINEAGE_TERMINAL", f"branch lineage {branch!r}/{lineage_id} is terminal")
    if marker.get("task_gid") != task_gid or marker.get("lineage_id") != lineage_id:
        fail("LINEAGE_REGISTRY_CHANGED", "repository-wide active lineage no longer matches the local task/branch/lineage identity")
    if marker.get("claim_token") != token or not marker.get("claim_active"):
        fail("OWNERSHIP_CLAIM_STALE", "repository-wide writer claim is not active for this exact token")
    return marker


def terminalize_lineage(
    runner: GitRunner,
    repo: Repository,
    *,
    task_gid: str,
    branch: str,
    lineage_id: str,
    disposition: str,
    expected_head: str,
) -> str:
    current = read_registry(runner, repo, branch)
    if current is None:
        fail("LINEAGE_REGISTRY_MISSING", f"cannot terminalize missing lineage registry for {branch}")
    sha, marker = current
    if marker.get("state") == "TERMINAL":
        if marker.get("task_gid") == task_gid and marker.get("lineage_id") == lineage_id:
            return sha
        fail("LINEAGE_REGISTRY_CHANGED", "terminal tombstone belongs to a different lineage")
    if marker.get("task_gid") != task_gid or marker.get("lineage_id") != lineage_id:
        fail("LINEAGE_REGISTRY_CHANGED", "active registry belongs to a different lineage")
    if marker.get("claim_active"):
        fail("OWNERSHIP_CLAIMED", "terminal cleanup refuses while the exact lineage still has an active writer claim")
    updated = dict(marker)
    updated.update(
        state="TERMINAL",
        disposition=disposition,
        terminal_head=require_full_sha(expected_head, "terminal lineage head"),
        terminalized_at=now_utc(),
        updated_at=now_utc(),
    )
    new_sha = _marker_commit(runner, repo, updated, parent=sha)
    _push_marker_cas(runner, repo, branch=branch, marker_sha=new_sha, expected_sha=sha)
    return new_sha



def record_lineage_head(
    runner: GitRunner, repo: Repository, *, task_gid: str, branch: str, lineage_id: str, token: str, head: str,
) -> str:
    head = require_full_sha(head, "published lineage head")
    current = read_registry(runner, repo, branch)
    if current is None:
        fail("LINEAGE_REGISTRY_MISSING", f"lineage registry missing for {branch}")
    sha, marker = current
    if marker.get("state") != "ACTIVE" or marker.get("task_gid") != task_gid or marker.get("lineage_id") != lineage_id:
        fail("LINEAGE_REGISTRY_CHANGED", "cannot record branch head on a different/terminal lineage")
    if marker.get("claim_token") != token or not marker.get("claim_active"):
        fail("OWNERSHIP_CLAIM_STALE", "publishing lineage head requires the exact active repository-wide claim")
    updated = dict(marker); updated.update(branch_head=head, updated_at=now_utc())
    new_sha = _marker_commit(runner, repo, updated, parent=sha)
    _push_marker_cas(runner, repo, branch=branch, marker_sha=new_sha, expected_sha=sha)
    return new_sha


def fence_lineage(
    runner: GitRunner, repo: Repository, *, task_gid: str, branch: str, lineage_id: str, token: str,
    reason: str, expected_head: str | None, observed_head: str | None,
) -> str:
    current = read_registry(runner, repo, branch)
    if current is None:
        fail("LINEAGE_REGISTRY_MISSING", f"cannot fence missing lineage registry for {branch}")
    sha, marker = current
    if marker.get("state") == "TERMINAL":
        return sha
    if marker.get("task_gid") != task_gid or marker.get("lineage_id") != lineage_id or marker.get("claim_token") != token:
        fail("LINEAGE_REGISTRY_CHANGED", "cannot fence a lineage that no longer matches the exact active claim")
    updated = dict(marker)
    updated.update(state="TERMINAL", disposition="fenced", fence_reason=reason, terminal_head=expected_head, observed_head=observed_head, terminalized_at=now_utc(), updated_at=now_utc(), claim_active=False)
    new_sha = _marker_commit(runner, repo, updated, parent=sha)
    _push_marker_cas(runner, repo, branch=branch, marker_sha=new_sha, expected_sha=sha)
    return new_sha

def pr_registry_ref(pr_number: int) -> str:
    if pr_number <= 0:
        fail("INVALID_PR", "PR number must be positive")
    return f"{PR_REGISTRY_REF_PREFIX}{pr_number}"


def _read_pr_registry(runner: GitRunner, repo: Repository, pr_number: int) -> tuple[str, dict[str, Any]] | None:
    ref = pr_registry_ref(pr_number)
    sha = remote_ref_sha(runner, repo, ref, allow_missing=True)
    if sha is None:
        return None
    payload = _read_marker_object(runner, repo, sha)
    if payload.get("kind") != "pr" or payload.get("pr_number") != pr_number:
        fail("PR_REGISTRY_CORRUPT", f"PR registry ref {ref} has invalid identity")
    return sha, payload


def acquire_pr_registry_claim(
    runner: GitRunner, repo: Repository, *, pr_number: int, task_gid: str, branch: str,
    lineage_id: str, token: str, agent_id: str,
) -> None:
    ref = pr_registry_ref(pr_number)
    existing = _read_pr_registry(runner, repo, pr_number)
    if existing is None:
        marker = {
            "schema": REGISTRY_SCHEMA, "kind": "pr", "state": "ACTIVE", "pr_number": pr_number,
            "task_gid": task_gid, "branch": branch, "lineage_id": lineage_id,
            "claim_token": token, "claim_agent_id": agent_id, "claim_active": True,
            "created_at": now_utc(), "updated_at": now_utc(),
        }
        new_sha = _marker_commit(runner, repo, marker)
        result = runner.run(repo.source_top, "push", f"--force-with-lease={ref}:", repo.origin_url, f"{new_sha}:{ref}", check=False)
        if result.returncode != 0:
            observed = _read_pr_registry(runner, repo, pr_number)
            if observed is not None:
                _, winner = observed
                fail("PR_LINEAGE_CONFLICT", f"PR #{pr_number} is already bound to branch {winner.get('branch')!r}/lineage {winner.get('lineage_id')}")
            fail("PR_REGISTRY_CAS_FAILED", f"could not admit PR #{pr_number} registry")
        return
    sha, marker = existing
    if marker.get("lineage_id") != lineage_id or marker.get("branch") != branch or marker.get("task_gid") != task_gid:
        fail("PR_LINEAGE_CONFLICT", f"PR #{pr_number} is permanently bound to branch {marker.get('branch')!r}/lineage {marker.get('lineage_id')}")
    if marker.get("claim_active"):
        fail("OWNERSHIP_CLAIMED", f"PR #{pr_number} already has an active repository-wide writer claim")
    updated = dict(marker)
    updated.update(claim_token=token, claim_agent_id=agent_id, claim_active=True, updated_at=now_utc())
    new_sha = _marker_commit(runner, repo, updated, parent=sha)
    _push_marker_cas(runner, repo, branch=branch, marker_sha=new_sha, expected_sha=sha) if False else None
    result = runner.run(repo.source_top, "push", f"--force-with-lease={ref}:{sha}", repo.origin_url, f"{new_sha}:{ref}", check=False)
    if result.returncode != 0:
        fail("PR_REGISTRY_CAS_FAILED", f"repository-wide PR #{pr_number} claim CAS failed")


def release_pr_registry_claim(
    runner: GitRunner, repo: Repository, *, pr_number: int, lineage_id: str, token: str,
) -> None:
    current = _read_pr_registry(runner, repo, pr_number)
    if current is None:
        fail("PR_REGISTRY_MISSING", f"PR #{pr_number} registry disappeared")
    sha, marker = current
    if marker.get("lineage_id") != lineage_id or marker.get("claim_token") != token or not marker.get("claim_active"):
        fail("PR_REGISTRY_CHANGED", f"PR #{pr_number} registry claim changed before release")
    updated = dict(marker); updated.update(claim_active=False, updated_at=now_utc())
    new_sha = _marker_commit(runner, repo, updated, parent=sha)
    ref = pr_registry_ref(pr_number)
    result = runner.run(repo.source_top, "push", f"--force-with-lease={ref}:{sha}", repo.origin_url, f"{new_sha}:{ref}", check=False)
    if result.returncode != 0:
        fail("PR_REGISTRY_CAS_FAILED", f"repository-wide PR #{pr_number} release CAS failed")
