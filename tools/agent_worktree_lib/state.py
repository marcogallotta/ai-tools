from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .common import (
    AGENT_ID_RE, EXPECTED_ORIGIN_ID, EXPECTED_REPOSITORY, SCHEMA_VERSION,
    AgentWorktreeError, fail, now_utc, require_agent_id, require_full_sha, require_task_gid,
)
from .asana_v2 import REGISTERED_V2_PROJECTS, classify_registered_v2_project

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
    state = {
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
    serialized_authority = os.environ.get("DISH_AGENT_ASSIGNMENT_AUTHORITY")
    if serialized_authority:
        try:
            authority = json.loads(serialized_authority)
        except json.JSONDecodeError:
            fail("MUTATION_ASSIGNMENT_MISMATCH", "live claim assignment authority is malformed")
        if not isinstance(authority, dict):
            fail("MUTATION_ASSIGNMENT_MISMATCH", "live claim assignment authority is not an object")
        state["repository_assignment_authority"] = authority
    return state


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




LEGACY_WORKFLOW_PROJECT = {
    "gid": "1217381674871544",
    "name": "Dish — Workflow",
    "sections": {"In Progress", "Review / Integration"},
}
HANDOFF_MARKER = "dish-implementation-handoff:v1"
STALE_HANDOFF_MARKER = "dish-stale-handoff:v1"
_HANDOFF_RE = re.compile(
    rf"<!--\s*{re.escape(HANDOFF_MARKER)}\s+handoff=(?P<id>[0-9a-f]{{16}})\s+"
    r"task=(?P<task>\d+)\s+role=(?P<role>[A-Za-z-]+)\s+at=(?P<at>[^\s]+)\s*-->"
)
_STALE_RE = re.compile(rf"<!--\s*{re.escape(STALE_HANDOFF_MARKER)}\s+handoff=(?P<id>[0-9a-f]{{16}})\s*-->")


def _asana_json(path: str, label: str, *, environment: Mapping[str, str] | None = None) -> Any:
    asana = Path.home().resolve() / ".local" / "bin" / "asana"
    try:
        result = subprocess.run(
            [str(asana), "raw", "GET", path],
            capture_output=True,
            text=True,
            check=False,
            env=dict(environment) if environment is not None else None,
        )
    except OSError as exc:
        fail("MUTATION_TASK_AUTHORITY_UNAVAILABLE", f"cannot read {label}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        fail("MUTATION_TASK_AUTHORITY_UNAVAILABLE", f"{label} read failed: {detail or f'exit {result.returncode}'}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        fail("MUTATION_TASK_AUTHORITY_INVALID", f"{label} read returned malformed JSON")
    if isinstance(payload, dict) and set(payload) == {"data"}:
        return payload["data"]
    return payload


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _handoff_source(assignment: Mapping[str, Any]) -> str:
    source = (
        "dish-prelaunch:v1 repository=marcogallotta/ai-tools "
        f"task={assignment['task_gid']} assignment=implementation host=local "
        f"branch={assignment['branch']} base_ref={assignment['base_ref']} "
        f"base_sha={assignment['base_sha']} existing_pr="
    )
    if assignment.get("pr_number") is None:
        return source + "none"
    return source + f"{assignment['pr_number']} expected_head={assignment['pr_head']}"


def _handoff_identity(task_gid: str, timestamp: datetime, source: str) -> str:
    raw = f"{task_gid}\0Implementation\0{timestamp.astimezone(timezone.utc).isoformat()}\0{source}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _verified_ready_handoff(task_gid: str, assignment: Mapping[str, Any]) -> dict[str, Any]:
    story_environment = os.environ.copy()
    story_environment.update(
        {
            "DISH_HANDOFF_EXPECTED_BRANCH": str(assignment["branch"]),
            "DISH_HANDOFF_EXPECTED_BASE_REF": str(assignment["base_ref"]),
            "DISH_HANDOFF_EXPECTED_BASE": str(assignment["base_sha"]),
            "DISH_HANDOFF_EXPECTED_PR": str(assignment.get("pr_number") or ""),
            "DISH_HANDOFF_EXPECTED_HEAD": str(assignment.get("pr_head") or ""),
        }
    )
    stories = _asana_json(
        f"/tasks/{task_gid}/stories?opt_fields=gid,created_at,text,resource_subtype&limit=100",
        "live task handoff stories",
        environment=story_environment,
    )
    if not isinstance(stories, list):
        fail("MUTATION_READY_HANDOFF_INVALID", "live task handoff stories are not a list")
    source = _handoff_source(assignment)
    expected_lines = {
        "AUTHORIZED IMPLEMENTATION HANDOFF",
        f"Task: {task_gid}",
        "Target role: Implementation",
        f"Source: {source}",
        f"Branch: {assignment['branch']}",
        f"Base: {assignment['base_sha']}",
        f"PR: {assignment.get('pr_number') if assignment.get('pr_number') is not None else 'not yet known'}",
        f"Head: {assignment.get('pr_head') if assignment.get('pr_head') is not None else 'not yet known'}",
        "— Dish Agent: Development Workflow | repository control plane",
    }
    matches: list[dict[str, Any]] = []
    stale_ids = {
        match.group("id")
        for story in stories
        for match in _STALE_RE.finditer(str(story.get("text") or ""))
    }
    for story in stories:
        text = str(story.get("text") or "")
        markers = list(_HANDOFF_RE.finditer(text))
        if len(markers) != 1:
            continue
        marker = markers[0]
        if marker.group("task") != task_gid or marker.group("role") != "Implementation":
            continue
        handoff_at = _parse_time(marker.group("at"))
        created_at = _parse_time(story.get("created_at"))
        if handoff_at is None or created_at is None or abs((created_at - handoff_at).total_seconds()) > 300:
            continue
        handoff_id = _handoff_identity(task_gid, handoff_at, source)
        if marker.group("id") != handoff_id or handoff_id in stale_ids:
            continue
        if expected_lines | {f"Handoff time: {handoff_at.isoformat()}"} <= set(text.splitlines()):
            matches.append({"handoff_id": handoff_id, "source": source, "story_gid": str(story.get("gid") or "")})
    if len(matches) != 1:
        fail("MUTATION_READY_HANDOFF_INVALID", "Ready/first-claim admission requires exactly one current matching LOCAL_IMPLEMENTATION handoff")
    authority = matches[0]
    authority["digest"] = hashlib.sha256(source.encode()).hexdigest()
    authority["assignment"] = dict(assignment)
    return authority


def _project_sections(project_gid: str) -> set[str]:
    payload = _asana_json(f"/projects/{project_gid}/sections?opt_fields=gid,name&limit=100", "live project sections")
    if not isinstance(payload, list):
        fail("MUTATION_TASK_AUTHORITY_INVALID", "live project sections are not a list")
    return {str(item.get("name") or "") for item in payload if isinstance(item, dict)}


def _live_repository_mutation_task(
    task_gid: str,
    *,
    assignment: Mapping[str, Any] | None = None,
    admitted_authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the existing Asana lifecycle authority for one exact task.

    The worktree tool does not interpret notes or create a second workflow state.
    It consumes only the current structured membership maintained by the canonical
    Development Workflow project.  Claim and writer action boundaries are the
    material action-class transitions at which this live witness is refreshed.
    """
    task_gid = require_task_gid(task_gid)
    query = (
        f"/tasks/{task_gid}?opt_fields=gid,completed,"
        "memberships.project.gid,memberships.project.name,"
        "memberships.section.gid,memberships.section.name"
    )
    task = _asana_json(query, "live task authority")
    if not isinstance(task, dict) or str(task.get("gid") or "") != task_gid:
        fail("MUTATION_TASK_AUTHORITY_INVALID", "live task authority does not match the requested task")
    if bool(task.get("completed")):
        fail("MUTATION_TASK_MODE_BLOCKED", "completed task does not permit repository Implementation")
    owning_gids = set(REGISTERED_V2_PROJECTS) | {LEGACY_WORKFLOW_PROJECT["gid"]}
    matches = []
    for membership in task.get("memberships") or []:
        if not isinstance(membership, dict):
            continue
        project = membership.get("project")
        section = membership.get("section")
        if isinstance(project, dict) and str(project.get("gid") or "") in owning_gids:
            matches.append((project, section))
    if len(matches) != 1:
        fail(
            "MUTATION_TASK_AUTHORITY_INVALID",
            "task must have exactly one current repository-mutation-owning project membership",
        )
    project, section = matches[0]
    section_name = str(section.get("name") or "").strip() if isinstance(section, dict) else ""
    project_gid = str(project.get("gid") or "")
    if project_gid == LEGACY_WORKFLOW_PROJECT["gid"]:
        if str(project.get("name") or "") != LEGACY_WORKFLOW_PROJECT["name"]:
            fail("MUTATION_TASK_AUTHORITY_INVALID", "legacy owning project identity is contradictory")
        if section_name not in LEGACY_WORKFLOW_PROJECT["sections"]:
            fail("MUTATION_TASK_MODE_BLOCKED", f"current task mode {section_name or 'unknown'!r} does not permit repository Implementation")
        return {"task": task, "assignment_authority": admitted_authority}

    classify_registered_v2_project(project, _project_sections(project_gid))
    if section_name not in {"Ready", "Under Development"}:
        fail(
            "MUTATION_TASK_MODE_BLOCKED",
            f"current task mode {section_name or 'unknown'!r} does not permit repository Implementation",
        )
    authority = dict(admitted_authority) if admitted_authority is not None else None
    if authority is None:
        if assignment is None:
            fail("MUTATION_READY_HANDOFF_REQUIRED", "first repository claim requires an exact LOCAL_IMPLEMENTATION assignment handoff")
        authority = _verified_ready_handoff(task_gid, assignment)
    stored_assignment = authority.get("assignment") if isinstance(authority, dict) else None
    if assignment is not None and isinstance(stored_assignment, dict):
        stable_keys = ("task_gid", "branch", "base_ref", "base_sha")
        compatible = all(stored_assignment.get(key) == assignment.get(key) for key in stable_keys)
        if stored_assignment.get("pr_number") is not None:
            compatible = compatible and (
                stored_assignment.get("pr_number") == assignment.get("pr_number")
                and stored_assignment.get("pr_head") == assignment.get("pr_head")
            )
        if not compatible:
            fail("MUTATION_ASSIGNMENT_MISMATCH", "live claim assignment differs from the exact admitted handoff assignment")
    return {"task": task, "assignment_authority": authority}


def require_repository_mutation_identity(
    agent_id: str,
    task_gid: str,
    *,
    assignment: Mapping[str, Any] | None = None,
    admitted_authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed when local identity does not authorize repository Implementation.

    The identity file is a recovery projection, not authority creation.  This check
    combines the exact local Implementation assignment projection with the existing
    live Asana task/lifecycle authority.  Neither projection can authorize mutation
    alone, and missing exact task binding is never upgraded into authority.
    """
    payload = validate_agent_state(agent_id)
    assert payload is not None
    role = str(payload.get("role") or "").strip().lower().replace("_", "-")
    if role != "implementation":
        fail(
            "MUTATION_AUTHORITY_REQUIRED",
            f"active agent role {role or 'unknown'!r} is not Implementation; repository mutation refused",
        )
    owning = payload.get("owning_task_gid")
    if owning is None:
        fail(
            "MUTATION_AUTHORITY_TASK_REQUIRED",
            "active Implementation identity has no exact owning task binding",
        )
    if require_task_gid(str(owning)) != require_task_gid(task_gid):
        fail(
            "MUTATION_AUTHORITY_TASK_MISMATCH",
            f"active Implementation identity is bound to task {owning}, not requested task {task_gid}",
        )
    live = _live_repository_mutation_task(
        task_gid, assignment=assignment, admitted_authority=admitted_authority
    )
    payload["repository_assignment_authority"] = live.get("assignment_authority")
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
