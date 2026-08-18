from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .common import EXPECTED_REPOSITORY, fail, now_utc, require_agent_id, require_full_sha, require_task_gid
from .state import agent_state_path, atomic_write_json, read_json_object

LAUNCH_SCHEMA = "dish-local-implementation-launch-v1"
LAUNCH_MAX_AGE_SECONDS = 15 * 60
PROJECT_RE = re.compile(r"[0-9]+")
LAUNCH_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,200}")


def launch_provenance_root() -> Path:
    root = Path.home().resolve() / ".local" / "state" / "dish" / "launch-provenance"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _parse_time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance issued_at must be an ISO timestamp")
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance issued_at must be an ISO timestamp")
    if parsed.tzinfo is None:
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance issued_at must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _secure_provenance_path(path: Path) -> Path:
    root = launch_provenance_root()
    if path.is_symlink():
        fail("LAUNCH_PROVENANCE_INVALID", f"launch provenance must not be a symlink: {path}")
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        fail("LAUNCH_PROVENANCE_MISSING", f"cannot resolve launch provenance: {exc}")
    try:
        resolved.relative_to(root)
    except ValueError:
        fail("LAUNCH_PROVENANCE_INVALID", f"launch provenance must live under {root}")
    mode = resolved.stat().st_mode & 0o777
    if mode & 0o077:
        fail("LAUNCH_PROVENANCE_PERMISSIONS", f"launch provenance must not be group/world accessible: {resolved}")
    return resolved


def _load(path: Path) -> dict[str, Any]:
    secure = _secure_provenance_path(path)
    try:
        payload = json.loads(secure.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail("LAUNCH_PROVENANCE_INVALID", f"cannot read launch provenance JSON: {exc}")
    if not isinstance(payload, dict):
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance must contain exactly one JSON object")
    return payload


def _validate_shape(
    payload: Mapping[str, Any],
    *,
    agent_id: str,
    task_gid: str,
    branch: str,
    repo_path: Path,
    pr: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if payload.get("schema") != LAUNCH_SCHEMA:
        fail("LAUNCH_PROVENANCE_INVALID", f"launch provenance schema must be {LAUNCH_SCHEMA}")
    if payload.get("repository") != EXPECTED_REPOSITORY:
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance repository does not match marcogallotta/ai-tools")
    if require_agent_id(str(payload.get("agent_id") or "")) != agent_id:
        fail("LAUNCH_PROVENANCE_MISMATCH", "launch provenance agent_id does not match the claimed local agent")
    host = str(payload.get("host") or "")
    source = str(payload.get("identity_source") or "")
    expected_source = {"codex": "codex_thread_id", "claude": "claude_session_id"}.get(host)
    if expected_source is None or source != expected_source:
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance host/identity_source must be codex/codex_thread_id or claude/claude_session_id")
    if host == "codex":
        native = os.environ.get("CODEX_THREAD_ID")
        if native and native != agent_id:
            fail("LAUNCH_PROVENANCE_MISMATCH", "CODEX_THREAD_ID conflicts with launch provenance agent_id")
    if str(payload.get("role") or "").strip().lower().replace("_", "-") != "implementation":
        fail("LAUNCH_PROVENANCE_AUTHORITY", "local continuation launch provenance must bind exactly the Implementation role")
    if require_task_gid(str(payload.get("task_gid") or "")) != task_gid:
        fail("LAUNCH_PROVENANCE_MISMATCH", "launch provenance task does not match the claimed task")
    project_gid = str(payload.get("owning_project_gid") or "")
    if PROJECT_RE.fullmatch(project_gid) is None:
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance owning_project_gid must contain digits only")
    if str(payload.get("branch") or "") != branch:
        fail("LAUNCH_PROVENANCE_MISMATCH", "launch provenance branch does not match the claimed branch")
    if pr is None:
        fail("LAUNCH_PROVENANCE_PR_REQUIRED", "proven local Implementation continuation requires exact PR/head identity")
    try:
        number = int(payload.get("pr_number"))
    except (TypeError, ValueError):
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance pr_number must be numeric")
    if number != int(pr["number"]) or require_full_sha(str(payload.get("pr_head") or ""), "launch provenance PR head") != str(pr["head"]):
        fail("LAUNCH_PROVENANCE_MISMATCH", "launch provenance PR/head does not match the current claim")
    workspace = Path(str(payload.get("workspace") or "")).expanduser()
    try:
        workspace = workspace.resolve(strict=True)
    except OSError as exc:
        fail("LAUNCH_PROVENANCE_INVALID", f"launch provenance workspace is not resolvable: {exc}")
    if workspace != repo_path.resolve():
        fail("LAUNCH_PROVENANCE_MISMATCH", "launch provenance workspace does not match the verified repository checkout")
    launch_id = str(payload.get("launch_id") or "")
    if LAUNCH_ID_RE.fullmatch(launch_id) is None:
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance launch_id is invalid")
    launcher = str(payload.get("launcher") or "").strip()
    if not launcher:
        fail("LAUNCH_PROVENANCE_INVALID", "launch provenance launcher is required")

    issued = _parse_time(payload.get("issued_at"))
    current = dt.datetime.now(dt.timezone.utc)
    age = (current - issued).total_seconds()
    if age < -60:
        fail("LAUNCH_PROVENANCE_STALE", "launch provenance timestamp is in the future")
    if age > LAUNCH_MAX_AGE_SECONDS:
        fail("LAUNCH_PROVENANCE_STALE", "launch provenance is older than the bounded launch/claim window")

    return {
        "schema": LAUNCH_SCHEMA,
        "launch_id": launch_id,
        "launcher": launcher,
        "host": host,
        "identity_source": source,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "agent_id": agent_id,
        "role": "implementation",
        "task_gid": task_gid,
        "owning_project_gid": project_gid,
        "branch": branch,
        "pr_number": int(pr["number"]),
        "pr_head": str(pr["head"]),
        "workspace": str(repo_path.resolve()),
    }


def bind_identity_from_launch_provenance(
    path: Path,
    *,
    agent_id: str,
    task_gid: str,
    branch: str,
    repo_path: Path,
    pr: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Atomically bind real launch identity to already-authorized lineage; never invent authority."""
    payload = _validate_shape(
        _load(path),
        agent_id=agent_id,
        task_gid=task_gid,
        branch=branch,
        repo_path=repo_path,
        pr=pr,
    )
    identity_path = agent_state_path(agent_id)
    prior: dict[str, Any] | None = None
    if identity_path.exists():
        prior = read_json_object(identity_path, "per-agent identity state")
        if prior.get("agent_id") != agent_id:
            fail("LAUNCH_PROVENANCE_MISMATCH", "existing identity file agent_id conflicts with launch provenance")
        prior_role = str(prior.get("role") or "").strip().lower().replace("_", "-")
        if prior_role and prior_role != "implementation":
            fail("LAUNCH_PROVENANCE_AUTHORITY", "existing identity role conflicts with Implementation launch provenance")
        for key, expected in (("owning_task_gid", task_gid), ("owning_project_gid", payload["owning_project_gid"])):
            existing = prior.get(key)
            if existing is not None and str(existing) != str(expected):
                fail("LAUNCH_PROVENANCE_AUTHORITY", f"existing identity {key} conflicts with launch provenance")
        active = prior.get("active_worktree")
        if isinstance(active, dict):
            if active.get("task_gid") not in {None, task_gid} or active.get("branch") not in {None, branch}:
                fail("LAUNCH_PROVENANCE_AUTHORITY", "existing identity active_worktree conflicts with launch lineage")

    identity = dict(prior or {})
    identity.update(
        {
            "agent_id": agent_id,
            "role": "implementation",
            "assigned_at": identity.get("assigned_at") or payload["issued_at"],
            "workspace": payload["workspace"],
            "owning_task_gid": task_gid,
            "owning_project_gid": payload["owning_project_gid"],
            "launch_provenance": {
                **payload,
                "consumed_at": now_utc(),
            },
        }
    )
    atomic_write_json(identity_path, identity)
    return prior, identity


def restore_identity(agent_id: str, prior: dict[str, Any] | None) -> None:
    path = agent_state_path(agent_id)
    if prior is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    else:
        atomic_write_json(path, prior)
