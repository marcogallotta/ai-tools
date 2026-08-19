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
V3_SHADOW_SCHEMA = "dish-pr-lifecycle-v3-shadow-v1"
V3_SHADOW_MODE = "SHADOW"
V3_AUTHORITATIVE_LANDING_PATH = "integration-v1a-local-fenced"

_PHASE_BY_GITHUB_STATE = {
    "authoring_implementation_in_progress": "IMPLEMENTATION_IN_PROGRESS",
    "implementation_continuation_required": "IMPLEMENTATION_IN_PROGRESS",
    "review_ready": "READY_FOR_REVIEW",
    "review_in_progress": "REVIEW_IN_PROGRESS",
    "review_passed_evaluating_gates": "REVIEW_PASS",
    "local_implementation_completion_required": "IMPLEMENTATION_COMPLETION_REQUIRED",
    "local_certification_required": "REVIEW_PASS",
    "waiting_ci_certification": "REVIEW_PASS",
    "waiting_external_dependency": "BLOCKED_EXTERNAL",
    "waiting_infrastructure": "BLOCKED_EXTERNAL",
    "integration_ready": "INTEGRATION_READY",
    "merging_integration_in_progress": "INTEGRATION_IN_PROGRESS",
    "merged": "MERGED",
    "closed_superseded": "CLOSED",
}


def _task_index(tasks: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(task.get("gid")): dict(task)
        for task in tasks
        if task.get("gid")
    }


def _completion_for(pr: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]]) -> str:
    task_ids = [str(value) for value in pr.get("task_ids") or [] if value]
    if not task_ids:
        return "UNKNOWN"
    values: list[bool] = []
    for gid in task_ids:
        task = tasks.get(gid)
        if task is None or task.get("error"):
            return "UNKNOWN"
        values.append(bool(task.get("completed")))
    return "COMPLETE" if values and all(values) else "INCOMPLETE"


def _rollouts_for(pr: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for gid in pr.get("task_ids") or []:
        task = tasks.get(str(gid))
        rollout = task.get("rollout") if task else None
        if isinstance(rollout, Mapping):
            result.append(dict(rollout))
    return result


def _phase_for(pr: Mapping[str, Any]) -> str:
    state = str(pr.get("state") or "")
    if state == "changes_requested_fix_in_progress":
        active = {
            str(lease.get("phase") or "")
            for lease in pr.get("active_leases") or []
            if isinstance(lease, Mapping)
        }
        return "FIXES_IN_PROGRESS" if active & {"fix", "implementation"} else "REVIEW_BLOCK"
    return _PHASE_BY_GITHUB_STATE.get(state, "UNKNOWN")


def _default_source(pr: Mapping[str, Any]) -> dict[str, Any]:
    state = str(pr.get("state") or "")
    base = str(pr.get("base") or "")
    if state == "merged" and base == "main":
        return {
            "state": "LANDED",
            "ultimate_target": "main",
            "publication_state": "landed",
            "provenance": "github-pr-merged-to-declared-base",
        }
    if base == "main":
        return {
            "state": "NOT_LANDED",
            "ultimate_target": "main",
            "publication_state": "open" if state != "closed_superseded" else "closed",
            "provenance": "github-pr-state",
        }
    return {
        "state": "UNKNOWN",
        "ultimate_target": None,
        "publication_state": "merged" if state == "merged" else state or "unknown",
        "provenance": "ultimate-target-not-declared",
    }


def _default_runtime() -> dict[str, Any]:
    return {
        "active": "UNKNOWN",
        "operational": "UNKNOWN",
        "provenance": "runtime-not-observed-by-this-slice",
    }


def _truth_conflicts(
    pr: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    completion: str,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    unreadable = False
    for task in pr.get("asana") or []:
        if isinstance(task, Mapping) and task.get("error"):
            unreadable = True
            conflicts.append({
                "kind": "ASANA_UNREADABLE",
                "truth": "UNKNOWN",
                "task": task.get("gid"),
                "detail": str(task.get("error")),
            })
    if pr.get("task_ids") and completion == "UNKNOWN" and not unreadable:
        conflicts.append({
            "kind": "ASANA_WORK_STATE_UNKNOWN",
            "truth": "UNKNOWN",
            "detail": "Linked Asana work state was not available from the pure-read task observation.",
        })
    if completion == "COMPLETE" and source.get("state") == "NOT_LANDED":
        conflicts.append({
            "kind": "ASANA_COMPLETE_SOURCE_NOT_LANDED",
            "truth": "CONTRADICTION",
            "detail": "Asana marks linked work complete while GitHub source evidence says the ultimate target is not landed.",
        })
    return conflicts


def _rollout_accepted(rollouts: list[Mapping[str, Any]]) -> bool:
    return bool(rollouts) and all(
        rollout.get("complete") is True
        and all(stage.get("state") == "ACCEPTED" for stage in rollout.get("stages") or [])
        for rollout in rollouts
    )


def _combined_state(
    *,
    phase: str,
    source: Mapping[str, Any],
    runtime: Mapping[str, Any],
    completion: str,
    rollouts: list[Mapping[str, Any]],
    conflicts: list[Mapping[str, Any]],
    operator_action: str | None,
) -> str:
    if any(item.get("truth") == "CONTRADICTION" for item in conflicts):
        return "CONTRADICTION"
    if source.get("lineage_state") == "MERGED_INTERMEDIATE_TARGET":
        return "MERGED_INTERMEDIATE_TARGET"
    if phase == "MERGED" and source.get("state") == "NOT_LANDED":
        return "MERGED_INTERMEDIATE_TARGET"
    if phase == "BLOCKED_EXTERNAL":
        return "BLOCKED_EXTERNAL"
    if operator_action:
        return "BLOCKED_ON_MARCO"
    if source.get("state") == "LANDED":
        accepted = _rollout_accepted(rollouts)
        if completion == "INCOMPLETE" or (rollouts and not accepted):
            return "POST_MERGE_ACTION_REQUIRED"
        if runtime.get("operational") == "NOT_OPERATIONAL":
            return "POST_MERGE_ACTION_REQUIRED"
        if (
            completion == "COMPLETE"
            and accepted
            and runtime.get("operational") == "OPERATIONAL"
        ):
            return "OPERATIONALLY_COMPLETE"
        return "LANDED_ON_MAIN" if source.get("ultimate_target") == "main" else "LANDED_ON_TARGET"
    return phase


def _queue_for_state(state: str) -> str:
    if state in {"IMPLEMENTATION_IN_PROGRESS", "IMPLEMENTATION_COMPLETION_REQUIRED", "FIXES_IN_PROGRESS"}:
        return "In Progress"
    if state in {"READY_FOR_REVIEW", "REVIEW_IN_PROGRESS"}:
        return "Review"
    if state in {
        "REVIEW_PASS", "INTEGRATION_READY", "INTEGRATION_IN_PROGRESS",
        "MERGED_INTERMEDIATE_TARGET",
    }:
        return "Integration"
    if state in {"REVIEW_BLOCK", "BLOCKED_EXTERNAL", "CONTRADICTION"}:
        return "Blocked"
    if state == "BLOCKED_ON_MARCO":
        return "Decision"
    if state in {"LANDED_ON_MAIN", "LANDED_ON_TARGET", "OPERATIONALLY_COMPLETE", "CLOSED"}:
        return "Recent"
    if state == "POST_MERGE_ACTION_REQUIRED":
        return "Ready"
    return "Ready"


def _state_label(state: str) -> str:
    return state.replace("_", " ").title()


def _v3_shadow_decision(
    pr: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    truth: str,
    operator_action: str | None,
) -> dict[str, Any]:
    """Project existing V1-A admission mechanics without acquiring write authority."""
    lifecycle_state = str(pr.get("state") or "")
    head = str(pr.get("head") or "")
    reviewed_head = str(pr.get("reviewed_head") or "")
    review_verdict = str(pr.get("review_verdict") or "")
    gate = pr.get("gate") if isinstance(pr.get("gate"), Mapping) else {}
    exact_head_review = bool(head) and review_verdict == "MERGE" and reviewed_head == head

    if lifecycle_state == "merged":
        decision = "NO_ACTION_ALREADY_MERGED"
        reason = "authoritative GitHub lifecycle already reports merged"
    elif lifecycle_state == "merging_integration_in_progress":
        decision = "OBSERVE_EXISTING_WRITER"
        reason = "an existing exact-head Integration writer is active; V3 shadow must not contend"
    elif lifecycle_state == "integration_ready":
        if source.get("state") != "NOT_LANDED":
            decision = "BLOCK_SOURCE_IDENTITY"
            reason = "Integration-ready candidate is not proven NOT_LANDED on its intended source lineage"
        elif not exact_head_review:
            decision = "BLOCK_EXACT_HEAD_REVIEW"
            reason = "Integration-ready candidate lacks matching exact-head MERGE review identity"
        else:
            decision = "WOULD_ADMIT_EXISTING_INTEGRATION"
            reason = (
                "current V1-A inspector has already proved exact-head Review and Integration gates; "
                "V3 shadow would admit the existing fenced Integration path if a later cutover authorized it"
            )
    else:
        decision = "WAIT_CURRENT_LIFECYCLE"
        reason = "candidate has not reached the current controller's Integration-ready boundary"

    return {
        "pr": int(pr["number"]),
        "head": head or None,
        "decision": decision,
        "reason": reason,
        "decision_scope": "current-v1a-mechanical-admission",
        "current_lifecycle_state": lifecycle_state,
        "current_review_verdict": review_verdict or None,
        "current_reviewed_head": reviewed_head or None,
        "current_gate_diagnosis": gate.get("diagnosis"),
        "current_operator_action": operator_action,
        "source_state": source.get("state"),
        "truth": truth,
        "write_authority": False,
        "mutation_permitted": False,
        "authoritative_landing_path": V3_AUTHORITATIVE_LANDING_PATH,
    }


def build_projection(
    values: Iterable[PRLifecycle],
    *,
    repository: str,
    tasks: Iterable[Mapping[str, Any]] = (),
    task_scope: Mapping[str, Any] | None = None,
    source_observation: Mapping[str, Any] | None = None,
    runtime_observation: Mapping[str, Any] | None = None,
    controller: Mapping[str, Any] | None = None,
    full_regression: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    prs = [value.json() for value in values]
    task_values = [dict(task) for task in tasks]
    tasks_by_gid = _task_index(task_values)
    sources = dict((source_observation or {}).get("pull_requests") or {})
    runtimes = dict((runtime_observation or {}).get("pull_requests") or {})
    queues: dict[str, list[int]] = {name: [] for name in ("Ready", "In Progress", "Review", "Integration", "Blocked", "Decision", "Recent")}
    drift: list[dict[str, Any]] = []
    coordinator: list[dict[str, Any]] = []
    baseline_owners: dict[str, dict[str, Any]] = {}
    resolved: list[dict[str, Any]] = []
    v3_shadow_decisions: list[dict[str, Any]] = []
    for pr in prs:
        source = dict(sources.get(str(pr["number"])) or sources.get(pr["number"]) or _default_source(pr))
        runtime = dict(runtimes.get(str(pr["number"])) or runtimes.get(pr["number"]) or _default_runtime())
        completion = _completion_for(pr, tasks_by_gid)
        rollouts = _rollouts_for(pr, tasks_by_gid)
        phase = _phase_for(pr)
        conflicts = _truth_conflicts(pr, source=source, completion=completion)
        truth = (
            "CONTRADICTION"
            if any(item.get("truth") == "CONTRADICTION" for item in conflicts)
            else "UNKNOWN"
            if conflicts or source.get("state") == "UNKNOWN" or completion == "UNKNOWN"
            else "CONSISTENT"
        )
        operator_action = str(pr.get("human_action") or "").strip() or None
        state = _combined_state(
            phase=phase,
            source=source,
            runtime=runtime,
            completion=completion,
            rollouts=rollouts,
            conflicts=conflicts,
            operator_action=operator_action,
        )
        v3_shadow_decisions.append(
            _v3_shadow_decision(
                pr,
                source=source,
                truth=truth,
                operator_action=operator_action,
            )
        )
        record = {
            "pr": int(pr["number"]),
            "head": pr.get("head"),
            "task_gids": list(pr.get("task_ids") or []),
            "state": state,
            "phase": phase,
            "source": source,
            "runtime": runtime,
            "completion": completion,
            "truth": truth,
            "operator_action": operator_action,
            "conflicts": conflicts,
            "provenance": {
                "github": "pr_lifecycle exact-head inspector",
                "asana": "direct task read" if completion != "UNKNOWN" else "unknown/incomplete",
                "runtime": runtime.get("provenance") or "unknown",
            },
        }
        if rollouts:
            record["rollouts"] = rollouts
        resolved.append(record)
        pr["github_state_label"] = pr.get("state_label")
        pr["resolved_state"] = state
        pr["source_state"] = source.get("state")
        pr["active_state"] = runtime.get("active") or "UNKNOWN"
        pr["operational_state"] = runtime.get("operational") or "UNKNOWN"
        pr["asana_completion"] = completion
        pr["truth_status"] = truth
        label_suffix = ""
        if truth == "UNKNOWN":
            label_suffix += " · Truth Unknown"
        if source.get("state") == "LANDED" and runtime.get("operational") == "UNKNOWN":
            label_suffix += " · Operational Unknown"
        pr["state_label"] = _state_label(state) + label_suffix
        queues[_queue_for_state(state)].append(int(pr["number"]))
        for conflict in conflicts:
            drift.append({
                "pr": pr["number"],
                "task": conflict.get("task"),
                "conflict": conflict["kind"],
                "detail": conflict.get("detail"),
                "repair_owner": "authority-owner",
            })
        if str(pr.get("state") or "") == "merged":
            for task in pr.get("asana") or []:
                if isinstance(task, Mapping) and task and task.get("completed") is False:
                    drift.append({
                        "pr": pr["number"],
                        "task": task.get("gid"),
                        "conflict": "GitHub merged while Asana task is incomplete",
                        "detail": "GitHub shows the PR merged while the linked Asana task remains incomplete.",
                        "repair_owner": "controller",
                    })
        dep = pr.get("external_dependency")
        if isinstance(dep, Mapping):
            key = str(dep.get("task_gid") or "")
            owner = baseline_owners.setdefault(key, {"task_gid": key, "main_sha": dep.get("main_sha"), "dependents": []})
            owner["dependents"].append(pr["number"])
        if operator_action:
            coordinator.append({"pr": pr["number"], "action": operator_action, "head": pr["head"]})
    return {
        "schema": SCHEMA,
        "repository": repository,
        "reconciled_at": generated_at.isoformat(),
        "pull_requests": prs,
        "tasks": task_values,
        "task_scope": dict(task_scope or {"status": "UNKNOWN", "projects": []}),
        "resolved_lifecycle": resolved,
        "source_observation": dict(source_observation or {}),
        "runtime_observation": dict(runtime_observation or {}),
        "queues": queues,
        "state_drift": drift,
        "controller": dict(controller or {}),
        "full_regression": dict(full_regression or {}),
        "current_main_corrective_owners": list(baseline_owners.values()),
        "rollouts": [task["rollout"] for task in task_values if isinstance(task.get("rollout"), Mapping)],
        "coordinator_actions": coordinator,
        "v3_shadow": {
            "schema": V3_SHADOW_SCHEMA,
            "mode": V3_SHADOW_MODE,
            "activation_authorized": False,
            "write_authority": False,
            "authoritative_landing_path": V3_AUTHORITATIVE_LANDING_PATH,
            "scope": "current-v1a-admission-equivalence",
            "human_hold_evaluation": "NOT_IMPLEMENTED_STAGE_1",
            "decisions": v3_shadow_decisions,
        },
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
