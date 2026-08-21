from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from .common import (
    GitRunner, Repository, WorktreeIdentity, current_symbolic_branch, fail,
    find_worktree_record, now_utc, path_is_within, require_full_sha, worktree_records,
)
from .repository import discover_repository
from .state import atomic_write_json, state_path, task_worktree_path, worktree_root

def validate_branch(runner: GitRunner, cwd: Path, branch: str) -> str:
    if not branch.startswith("agent/") or branch == "agent/":
        fail("INVALID_BRANCH", "owned branch must match agent/<short-task-slug>")
    result = runner.run(cwd, "check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0:
        fail("INVALID_BRANCH", f"invalid Git branch name: {branch!r}")
    return branch


def validate_base_ref(runner: GitRunner, cwd: Path, ref: str) -> str:
    if not ref.startswith("refs/heads/"):
        fail("INVALID_BASE_REF", "base ref must be a fully-qualified refs/heads/* ref")
    result = runner.run(cwd, "check-ref-format", ref, check=False)
    if result.returncode != 0:
        fail("INVALID_BASE_REF", f"invalid base ref: {ref!r}")
    return ref


def remote_ref_sha(
    runner: GitRunner,
    repo: Repository,
    ref: str,
    *,
    allow_missing: bool = False,
) -> str | None:
    result = runner.run(
        repo.source_top,
        "ls-remote",
        "--exit-code",
        repo.origin_url,
        ref,
        check=False,
    )
    if result.returncode == 2 and allow_missing:
        return None
    if result.returncode != 0:
        code = "REMOTE_REF_MISSING" if result.returncode == 2 else "REMOTE_QUERY_FAILED"
        detail = result.stderr.strip() or result.stdout.strip() or "no command output"
        fail(code, f"could not resolve exact remote ref {ref!r}; git ls-remote exited {result.returncode}: {detail}")
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        fail("REMOTE_REF_AMBIGUOUS", f"remote returned {len(rows)} rows for exact ref {ref!r}; expected one")
    parts = rows[0].split()
    if len(parts) != 2 or parts[1] != ref:
        fail("REMOTE_REF_AMBIGUOUS", f"remote returned an unexpected row for exact ref {ref!r}")
    return require_full_sha(parts[0], f"remote {ref} SHA")


def object_exists(runner: GitRunner, cwd: Path, sha: str) -> bool:
    result = runner.run(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    return result.returncode == 0


def ensure_commit_object(runner: GitRunner, repo: Repository, sha: str) -> None:
    sha = require_full_sha(sha, "commit SHA")
    if object_exists(runner, repo.source_top, sha):
        return
    result = runner.run(
        repo.source_top,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "--no-recurse-submodules",
        repo.origin_url,
        sha,
        check=False,
    )
    if result.returncode != 0:
        fail("BASE_FETCH_FAILED", f"failed to fetch exact commit object {sha} without updating a local ref")
    if not object_exists(runner, repo.source_top, sha):
        fail("BASE_FETCH_FAILED", f"exact commit object {sha} is still unavailable after fetch")


def remote_relation(runner: GitRunner, repo: Repository, branch: str, local_head: str) -> tuple[str, str | None]:
    remote_ref = f"refs/heads/{branch}"
    remote_head = remote_ref_sha(runner, repo, remote_ref, allow_missing=True)
    if remote_head is None:
        return "absent", None
    if remote_head == local_head:
        return "equal", remote_head
    ensure_commit_object(runner, repo, remote_head)
    remote_is_ancestor = runner.run(repo.source_top, "merge-base", "--is-ancestor", remote_head, local_head, check=False)
    if remote_is_ancestor.returncode == 0:
        return "local-ahead", remote_head
    if remote_is_ancestor.returncode not in (0, 1):
        fail("ANCESTRY_CHECK_FAILED", "could not compare local and remote owned branch heads")
    local_is_ancestor = runner.run(repo.source_top, "merge-base", "--is-ancestor", local_head, remote_head, check=False)
    if local_is_ancestor.returncode == 0:
        return "remote-ahead", remote_head
    if local_is_ancestor.returncode not in (0, 1):
        fail("ANCESTRY_CHECK_FAILED", "could not compare local and remote owned branch heads")
    return "divergent", remote_head


def resolve_repository_from_state(runner: GitRunner, state: dict[str, Any]) -> Repository:
    if state.get("lifecycle") != "active":
        fail("TASK_NOT_ACTIVE", f"task lifecycle is {state.get('lifecycle')!r}; active worktree operations are refused")
    worktree = Path(str(state["worktree_path"])).resolve()
    runner.check_candidate(worktree)
    if not worktree.is_dir():
        fail("WORKTREE_MISSING", f"recorded worktree path is missing: {worktree}")
    repo = discover_repository(runner, worktree)
    if repo.common_dir != Path(str(state["git_common_dir"])).resolve():
        fail("COMMON_DIR_MISMATCH", "recorded git common-dir does not match the actual repository common-dir")
    return repo


def verify_owned_worktree(runner: GitRunner, repo: Repository, state: dict[str, Any]) -> WorktreeIdentity:
    worktree = Path(str(state["worktree_path"])).resolve()
    expected_common = Path(str(state["git_common_dir"])).resolve()
    expected_git_dir = Path(str(state["git_dir"])).resolve()
    branch = str(state["branch"])
    validate_branch(runner, repo.source_top, branch)

    top = runner.path(worktree, "--show-toplevel")
    if top != worktree:
        fail("WORKTREE_TOPLEVEL_MISMATCH", f"recorded worktree must be the exact Git top-level: {worktree} != {top}")
    git_dir = runner.path(worktree, "--git-dir")
    common_dir = runner.path(worktree, "--git-common-dir")
    if git_dir != expected_git_dir:
        fail("GIT_DIR_MISMATCH", f"recorded per-worktree git-dir mismatch: {expected_git_dir} != {git_dir}")
    if common_dir != expected_common or common_dir != repo.common_dir:
        fail("COMMON_DIR_MISMATCH", "recorded/common repository git common-dir mismatch")
    actual_branch = current_symbolic_branch(runner, worktree)
    if actual_branch is None:
        fail("DETACHED_HEAD", "owned worktree is detached; automatic recovery is refused")
    if actual_branch != branch:
        fail("BRANCH_MISMATCH", f"owned worktree expected branch {branch!r}, found {actual_branch!r}")
    head = runner.sha(worktree, "HEAD")
    branch_head = runner.sha(worktree, f"refs/heads/{branch}")
    if branch_head != head:
        fail("BRANCH_HEAD_MISMATCH", "owned branch ref does not equal the checked-out worktree HEAD")
    record = find_worktree_record(worktree_records(runner, worktree), worktree)
    if record is None:
        fail("WORKTREE_REGISTRY", f"owned worktree is not registered: {worktree}")
    if record.get("branch") != f"refs/heads/{branch}":
        fail("WORKTREE_REGISTRY", "Git worktree registry branch does not match owned branch")
    if record.get("HEAD") != head:
        fail("WORKTREE_REGISTRY", "Git worktree registry HEAD does not match owned HEAD")
    dirty = bool(runner.run(worktree, "status", "--porcelain=v1", "--untracked-files=all").stdout)
    return WorktreeIdentity(worktree, git_dir, common_dir, branch, head, dirty)


def candidate_path_is_safe(repo: Repository, runner: GitRunner, candidate: Path) -> None:
    candidate = candidate.resolve()
    root = worktree_root().resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        fail("WORKTREE_PATH", f"task worktree path must be under DISH_WORKTREE_ROOT: {root}")
    if len(relative.parts) not in {1, 2}:
        fail("WORKTREE_PATH", f"task worktree path must be a legacy task path or task/lineage path under DISH_WORKTREE_ROOT: {root}")
    if path_is_within(candidate, repo.primary_top) or path_is_within(candidate, repo.common_dir):
        fail("WORKTREE_PATH", "owned worktree must live outside the shared primary checkout/common-dir")
    for record in worktree_records(runner, repo.source_top):
        raw = record.get("worktree")
        if not raw:
            continue
        registered = Path(raw).resolve()
        if candidate == registered:
            fail("WORKTREE_COLLISION", f"task worktree path is already registered: {candidate}")
        if path_is_within(candidate, registered):
            fail("WORKTREE_PATH", f"task worktree path may not be nested inside another registered worktree: {registered}")
    if candidate.exists():
        if not candidate.is_dir():
            fail("WORKTREE_COLLISION", f"task worktree path exists and is not a directory: {candidate}")
        try:
            next(candidate.iterdir())
        except StopIteration:
            return
        fail("WORKTREE_COLLISION", f"task worktree path already contains files but is not the owned registered worktree: {candidate}")


def branch_exists(runner: GitRunner, cwd: Path, branch: str) -> bool:
    result = runner.run(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if result.returncode not in (0, 1):
        fail("GIT_IDENTITY", f"could not determine whether local branch exists: {branch}")
    return result.returncode == 0


def owner_agent_id(state: dict[str, Any]) -> str | None:
    owner = state.get("owner")
    if isinstance(owner, dict):
        value = owner.get("agent_id")
        return str(value) if value is not None else None
    return None


def update_owner(state: dict[str, Any], agent_id: str | None, takeover: bool) -> None:
    current = owner_agent_id(state)
    if agent_id is None:
        return
    if current is not None and current != agent_id and not takeover:
        fail(
            "OWNER_MISMATCH",
            f"task record owner provenance is {current!r}; replacement agent {agent_id!r} requires explicit --takeover after handoff",
        )
    if takeover and current == agent_id:
        fail("TAKEOVER_NOT_NEEDED", f"agent {agent_id!r} already owns the local provenance record")
    if takeover and current is None:
        fail("TAKEOVER_NOT_NEEDED", "task record has no prior agent owner to take over from")
    if current != agent_id:
        history = state.setdefault("owner_history", [])
        if current is not None:
            history.append({"agent_id": current, "host": state.get("owner", {}).get("host"), "ended_at": now_utc()})
        state["owner"] = {"agent_id": agent_id, "host": socket.gethostname()}


def remote_and_target_observation(
    runner: GitRunner,
    repo: Repository,
    state: dict[str, Any],
    local_head: str,
) -> tuple[str, str | None, str, bool]:
    target = remote_ref_sha(runner, repo, str(state["base_ref"]))
    assert target is not None
    relation, remote_head = remote_relation(runner, repo, str(state["branch"]), local_head)
    moved = target != state["base_sha"]
    return relation, remote_head, target, moved


def record_observation(
    state: dict[str, Any],
    identity: WorktreeIdentity,
    relation: str,
    remote_head: str | None,
    target_head: str,
) -> None:
    stamp = now_utc()
    state["last_verified_at"] = stamp
    state["local_head"] = identity.head
    state["remote_owned_head"] = remote_head
    state["remote_relation"] = relation
    state["remote_checked_at"] = stamp
    state["target_current_head"] = target_head
    state["target_checked_at"] = stamp
    if relation == "equal":
        state["published_head"] = identity.head


def payload_from_state(
    command: str,
    state: dict[str, Any],
    identity: WorktreeIdentity,
    *,
    relation: str | None = None,
    remote_head: str | None = None,
    target_head: str | None = None,
) -> dict[str, Any]:
    target_head = target_head or state.get("target_current_head")
    return {
        "command": command,
        "ok": True,
        "task_gid": state["task_gid"],
        "branch": state["branch"],
        "worktree": state["worktree_path"],
        "state_path": str(state_path(state["task_gid"], state["branch"], state.get("lineage_id"))) if state.get("lineage_id") else str(state_path(state["task_gid"])),
        "base_ref": state["base_ref"],
        "base_sha": state["base_sha"],
        "local_head": identity.head,
        "dirty": identity.dirty,
        "owner_agent_id": owner_agent_id(state),
        "published_head": state.get("published_head"),
        "remote_owned_head": remote_head if relation is not None else state.get("remote_owned_head"),
        "remote_relation": relation if relation is not None else state.get("remote_relation"),
        "current_target_head": target_head,
        "target_moved": bool(target_head and target_head != state["base_sha"]),
        "next_action": f"tools/agent-worktree exec --task {state['task_gid']} -- <agent-command>",
    }


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    ordered = (
        "task_gid",
        "branch",
        "worktree",
        "base_ref",
        "base_sha",
        "local_head",
        "remote_owned_head",
        "remote_relation",
        "current_target_head",
        "target_moved",
        "dirty",
        "owner_agent_id",
        "state_path",
        "next_action",
    )
    for key in ordered:
        if key in payload:
            value = payload[key]
            print(f"{key}: {value if value is not None else '<none>'}")
