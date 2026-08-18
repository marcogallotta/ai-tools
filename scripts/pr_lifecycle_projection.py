"""Atomic, read-only normalized projection of lifecycle controller state."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
import uuid

from pr_lifecycle_support import PRLifecycle

SCHEMA = "dish-pr-lifecycle-projection-v1"


def _queue_for(pr: Mapping[str, Any]) -> str:
    state = str(pr.get("state") or "")
    if state in {"authoring_implementation_in_progress", "implementation_continuation_required", "changes_requested_fix_in_progress"}:
        return "In Progress"
    if state in {"review_ready", "review_in_progress"}:
        return "Review"
    if state in {"integration_ready", "merging_integration_in_progress", "review_passed_evaluating_gates", "waiting_ci_certification"}:
        return "Integration"
    if state in {"waiting_external_dependency", "waiting_infrastructure"}:
        return "Blocked"
    if state in {"merged", "closed_superseded"}:
        return "Recent"
    return "Ready"


def build_projection(
    values: Iterable[PRLifecycle],
    *,
    repository: str,
    tasks: Iterable[Mapping[str, Any]] = (),
    controller: Mapping[str, Any] | None = None,
    full_regression: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    prs = [value.json() for value in values]
    task_values = [dict(task) for task in tasks]
    queues: dict[str, list[int]] = {name: [] for name in ("Ready", "In Progress", "Review", "Integration", "Blocked", "Decision", "Recent")}
    drift: list[dict[str, Any]] = []
    coordinator: list[dict[str, Any]] = []
    baseline_owners: dict[str, dict[str, Any]] = {}
    for pr in prs:
        queues[_queue_for(pr)].append(int(pr["number"]))
        for task in pr.get("asana") or []:
            if task.get("error"):
                drift.append({"pr": pr["number"], "task": task.get("gid"), "conflict": "Asana task unreadable", "repair_owner": "controller"})
            if pr["state"] == "merged" and task and not task.get("completed"):
                drift.append({"pr": pr["number"], "task": task.get("gid"), "conflict": "GitHub merged while Asana task is incomplete", "repair_owner": "controller"})
        dep = pr.get("external_dependency")
        if isinstance(dep, Mapping):
            key = str(dep.get("task_gid") or "")
            owner = baseline_owners.setdefault(key, {"task_gid": key, "main_sha": dep.get("main_sha"), "dependents": []})
            owner["dependents"].append(pr["number"])
        if pr.get("human_action"):
            coordinator.append({"pr": pr["number"], "action": pr["human_action"], "head": pr["head"]})
    return {
        "schema": SCHEMA,
        "repository": repository,
        "reconciled_at": generated_at.isoformat(),
        "pull_requests": prs,
        "tasks": task_values,
        "queues": queues,
        "state_drift": drift,
        "controller": dict(controller or {}),
        "full_regression": dict(full_regression or {}),
        "current_main_corrective_owners": list(baseline_owners.values()),
        "rollouts": [task["rollout"] for task in task_values if isinstance(task.get("rollout"), Mapping)],
        "coordinator_actions": coordinator,
    }


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def read_projection(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("invalid lifecycle projection")
    return value
