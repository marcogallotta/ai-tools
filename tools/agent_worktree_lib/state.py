from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any

from .common import (
    AGENT_ID_RE, EXPECTED_ORIGIN_ID, EXPECTED_REPOSITORY, SCHEMA_VERSION,
    AgentWorktreeError, fail, now_utc, require_agent_id, require_full_sha, require_task_gid,
)

def state_root() -> Path:
    return Path.home().resolve() / ".local" / "state" / "dish" / "worktrees"


def _branch_digest(branch: str) -> str:
    return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:24]


def lineage_state_dir(task_gid: str) -> Path:
    return state_root() / require_task_gid(task_gid)


def lineage_state_path(task_gid: str, branch: str, lineage_id: str) -> Path:
    if not lineage_id or len(lineage_id) < 16:
        fail("LINEAGE_ID_INVALID", "lineage_id must be a durable non-empty incarnation id")
    return lineage_state_dir(task_gid) / f"{_branch_digest(branch)}-{lineage_id}.json"


def task_state_paths(task_gid: str) -> list[Path]:
    task_gid = require_task_gid(task_gid)
    paths: list[Path] = []
    legacy = state_root() / f"{task_gid}.json"
    if legacy.exists():
        paths.append(legacy)
    directory = lineage_state_dir(task_gid)
    if directory.is_dir():
        paths.extend(sorted(path for path in directory.glob("*.json") if path.is_file() and not path.is_symlink()))
    return paths


def state_path(task_gid: str, branch: str | None = None, lineage_id: str | None = None) -> Path:
    task_gid = require_task_gid(task_gid)
    if branch is None and os.environ.get("DISH_AGENT_CLAIM_TASK") == task_gid:
        branch = os.environ.get("DISH_AGENT_CLAIM_BRANCH")
        lineage_id = os.environ.get("DISH_AGENT_LINEAGE_ID")
    if branch is not None or lineage_id is not None:
        if branch is None or lineage_id is None:
            fail("LINEAGE_ID_INVALID", "branch and lineage_id must be supplied together")
        return lineage_state_path(task_gid, branch, lineage_id)
    paths = task_state_paths(task_gid)
    if not paths:
        return state_root() / f"{task_gid}.json"
    if len(paths) > 1:
        fail("LINEAGE_AMBIGUOUS", f"task {task_gid} has {len(paths)} durable lineages; exact branch/lineage identity is required")
    return paths[0]


def state_path_for_branch(task_gid: str, branch: str) -> Path | None:
    matches: list[Path] = []
    for path in task_state_paths(task_gid):
        try:
            payload = read_json_object(path, "task worktree state")
        except AgentWorktreeError:
            raise
        if payload.get("branch") == branch:
            matches.append(path)
    if len(matches) > 1:
        fail("LINEAGE_AMBIGUOUS", f"task {task_gid} has multiple durable states for branch {branch!r}")
    return matches[0] if matches else None


def agent_state_path(agent_id: str) -> Path:
    return Path.home().resolve() / ".local" / "state" / "dish" / "agents" / f"{require_agent_id(agent_id)}.json"


def worktree_root() -> Path:
    configured = os.environ.get("DISH_WORKTREE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().resolve() / ".local" / "share" / "dish" / "worktrees" / "ai-tools"


def task_worktree_path(task_gid: str, branch: str | None = None, lineage_id: str | None = None) -> Path:
    task_gid = require_task_gid(task_gid)
    if branch is None and os.environ.get("DISH_AGENT_CLAIM_TASK") == task_gid:
        branch = os.environ.get("DISH_AGENT_CLAIM_BRANCH")
        lineage_id = os.environ.get("DISH_AGENT_LINEAGE_ID")
    if branch is not None or lineage_id is not None:
        if branch is None or lineage_id is None:
            fail("LINEAGE_ID_INVALID", "branch and lineage_id must be supplied together for worktree resolution")
        return worktree_root() / task_gid / f"{_branch_digest(branch)}-{lineage_id}"
    return worktree_root() / task_gid


def new_active_task_state(
    *,
    task_gid: str,
    branch: str,
    worktree_path: Path,
    git_common_dir: Path,
    git_dir: Path,
    origin_id: str,
    base_ref: str,
    base_sha: str,
    agent_id: str | None,
    local_head: str,
    published_head: str | None,
    remote_owned_head: str | None,
    remote_relation: str,
    target_current_head: str,
    lineage_id: str | None = None,
) -> dict[str, Any]:
    """Build the single durable active-worktree state shape used by start/adopt."""
    stamp = now_utc()
    lineage_id = lineage_id or os.environ.get("DISH_AGENT_LINEAGE_ID")
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": {"full_name": EXPECTED_REPOSITORY, "origin_id": origin_id},
        "task_gid": task_gid,
        "branch": branch,
        "lineage_id": lineage_id,
        "worktree_path": str(worktree_path),
        "git_common_dir": str(git_common_dir),
        "git_dir": str(git_dir),
        "base_ref": base_ref,
        "base_sha": base_sha,
        "owner": {"agent_id": agent_id, "host": socket.gethostname()},
        "created_at": stamp,
        "last_verified_at": stamp,
        "local_head": local_head,
        "published_head": published_head,
        "remote_owned_head": remote_owned_head,
        "remote_relation": remote_relation,
        "remote_checked_at": stamp,
        "target_current_head": target_current_head,
        "target_checked_at": stamp,
        "pr_url": None,
        "pr_head": None,
        "lifecycle": "active",
        "disposition": None,
    }


def ensure_state_dir() -> None:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass


class TaskLock:
    def __init__(self, task_gid: str, branch: str | None = None, lineage_id: str | None = None):
        ensure_state_dir()
        task_gid = require_task_gid(task_gid)
        if branch is None and os.environ.get("DISH_AGENT_CLAIM_TASK") == task_gid:
            branch = os.environ.get("DISH_AGENT_CLAIM_BRANCH")
            lineage_id = os.environ.get("DISH_AGENT_LINEAGE_ID")
        if branch and lineage_id:
            self.path = state_root() / "locks" / f"{task_gid}-{_branch_digest(branch)}-{lineage_id}.lock"
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            self.path = state_root() / f"{task_gid}.lock"
        self.handle = None

    def __enter__(self) -> "TaskLock":
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.handle = os.fdopen(fd, "r+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        fail("STATE_AMBIGUOUS", f"{label} must not be a symlink: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail("STATE_MISSING", f"{label} does not exist: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        fail("STATE_INVALID", f"cannot read {label} {path}: {exc}")
    if not isinstance(payload, dict):
        fail("STATE_INVALID", f"{label} must contain a JSON object: {path}")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp.exists():
            temp.unlink()


def load_task_state(task_gid: str, *, branch: str | None = None, lineage_id: str | None = None) -> dict[str, Any]:
    path = state_path(task_gid, branch, lineage_id)
    state = read_json_object(path, "task worktree state")
    required = {
        "schema_version",
        "repository",
        "task_gid",
        "branch",
        "worktree_path",
        "git_common_dir",
        "git_dir",
        "base_ref",
        "base_sha",
        "owner",
        "created_at",
        "last_verified_at",
        "local_head",
        "published_head",
        "pr_url",
        "pr_head",
        "lifecycle",
    }
    missing = sorted(required - state.keys())
    if missing:
        fail("STATE_INVALID", "task worktree state is missing required field(s): " + ", ".join(missing))
    if state.get("schema_version") != SCHEMA_VERSION:
        fail("STATE_INVALID", f"unsupported task worktree state schema: {state.get('schema_version')!r}")
    if state.get("task_gid") != task_gid:
        fail("STATE_INVALID", "task worktree state task_gid does not match its filename")
    repository = state.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != EXPECTED_REPOSITORY or repository.get("origin_id") != EXPECTED_ORIGIN_ID:
        fail("STATE_INVALID", "task worktree state repository identity is not marcogallotta/ai-tools")
    require_full_sha(str(state.get("base_sha")), "stored base SHA")
    require_full_sha(str(state.get("local_head")), "stored local HEAD")
    branch = str(state.get("branch"))
    lineage_id = state.get("lineage_id")
    if lineage_id is not None and (not isinstance(lineage_id, str) or len(lineage_id) < 16):
        fail("STATE_INVALID", "task worktree state has invalid lineage_id")
    if not branch.startswith("agent/"):
        fail("STATE_INVALID", f"stored owned branch is not agent/*: {branch!r}")
    return state


def validate_agent_state(agent_id: str | None) -> dict[str, Any] | None:
    if agent_id is None:
        return None
    path = agent_state_path(agent_id)
    try:
        payload = read_json_object(path, "per-agent identity state")
    except AgentWorktreeError as exc:
        if exc.code == "STATE_MISSING":
            fail("AGENT_STATE_MISSING", f"--agent-id {agent_id!r} has no existing identity file at {path}")
        raise
    return payload


def set_agent_reference(agent_id: str, state: dict[str, Any]) -> None:
    path = agent_state_path(agent_id)
    payload = read_json_object(path, "per-agent identity state")
    payload["active_worktree"] = {
        "task_gid": state["task_gid"],
        "state_path": str(state_path(state["task_gid"], state["branch"], state.get("lineage_id"))) if state.get("lineage_id") else str(state_path(state["task_gid"])),
        "worktree": state["worktree_path"],
        "branch": state["branch"],
        "lineage_id": state.get("lineage_id"),
    }
    atomic_write_json(path, payload)


def clear_agent_reference(agent_id: str | None, task_gid: str, lineage_id: str | None = None) -> None:
    if agent_id is None:
        return
    path = agent_state_path(agent_id)
    if not path.exists() or path.is_symlink():
        return
    payload = read_json_object(path, "per-agent identity state")
    active = payload.get("active_worktree")
    if isinstance(active, dict) and active.get("task_gid") == task_gid:
        if lineage_id is None or active.get("lineage_id") in {None, lineage_id}:
            payload["active_worktree"] = None
            atomic_write_json(path, payload)
