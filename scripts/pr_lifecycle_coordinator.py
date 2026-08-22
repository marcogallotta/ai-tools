#!/usr/bin/env python3
"""Observe-only deterministic Coordinator frontier over Lifecycle V4.

The adapter consumes the existing lifecycle projection and authoritative duty
evidence.  It neither launches a model nor defines a wake identity: every
actionable result is identified only by Lifecycle V4 ``actionable_version``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from pr_lifecycle_duties import DUTIES, duty_for
from pr_lifecycle_v4 import POLICY_GENERATION, actionable_version


REPORT_SCHEMA = "dish-coordinator-observe-report-v1"
AUDIT_SCHEMA = "dish-coordinator-audit-v1"
ATTENTION_SECTIONS = frozenset({
    "needs research",
    "needs agentic review",
    "needs human review",
})
READY_SECTIONS = frozenset({"ready"})
BLOCKED_SECTIONS = frozenset({"waiting on dependency"})
FRICTION_PROJECT = "1217443500915644"
DEBT_PROJECT = "1217443501022227"
DEVELOPMENT_WORKFLOW_PROJECT = "1217419962189616"


@dataclass(frozen=True)
class CoordinatorProjection:
    actionable_cases: tuple[dict[str, Any], ...]
    report: dict[str, Any]


def audit_record(
    report: Mapping[str, Any], *, source_id: str, correlation_id: str
) -> dict[str, Any]:
    """Return one bounded record for the existing rotated V4 audit sink."""
    return {
        "schema": AUDIT_SCHEMA,
        "at": _now(),
        "event": "coordinator_frontier_evaluated",
        "source_id": source_id,
        "correlation_id": correlation_id,
        "duty_id": (report.get("duty") or {}).get("duty_id"),
        "frontier_digest": report.get("frontier_digest"),
        "authoritative_read": dict(report.get("authoritative_read") or {}),
        "counts": dict(report.get("counts") or {}),
        "decisions": list(report.get("decisions") or []),
        "cases": list(report.get("cases") or []),
        "wake_enabled": False,
        "model_turns_started": 0,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _membership_sections(task: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for membership in task.get("memberships") or []:
        if not isinstance(membership, Mapping):
            continue
        section = membership.get("section")
        if isinstance(section, Mapping):
            name = str(section.get("name") or "").strip().lower()
        else:
            name = str(section or "").strip().lower()
        if name:
            result.add(name)
    return result


def _membership_projects(task: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for membership in task.get("memberships") or []:
        if not isinstance(membership, Mapping):
            continue
        project = membership.get("project")
        gid = project.get("gid") if isinstance(project, Mapping) else project
        if gid:
            result.add(str(gid))
    return result


def _case(
    *,
    repository: str,
    reason_class: str,
    evidence: Mapping[str, Any],
    next_action: str,
    task: str | None = None,
    pr: int | None = None,
    head: str | None = None,
) -> dict[str, Any]:
    material = {
        "repository": repository,
        "reason_class": reason_class,
        "pr": pr,
        "task": task,
        "head": head,
        "evidence": dict(evidence),
    }
    return {
        "case_key": _digest(material)[:24],
        "reason_class": reason_class,
        "repository": repository,
        "pr": pr,
        "task": task,
        "head": head,
        "evidence": dict(evidence),
        "next_owner": "Coordinator",
        "next_action": next_action,
    }


def _existing_coordinator_cases(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    v3 = projection.get("v3") if isinstance(projection.get("v3"), Mapping) else {}
    attention = v3.get("attention") if isinstance(v3.get("attention"), Mapping) else {}
    return [
        dict(case)
        for case in attention.get("cases") or []
        if isinstance(case, Mapping) and case.get("next_owner") == "Coordinator"
    ]


def _hourly_cases(projection: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scope = projection.get("task_scope") if isinstance(projection.get("task_scope"), Mapping) else {}
    if scope.get("status") != "COMPLETE":
        return [], [{
            "result": "suppressed",
            "reason": "authoritative_task_scope_unavailable",
            "detail": scope.get("reason") or scope.get("errors"),
        }]

    repository = str(projection.get("repository") or "")
    cases = _existing_coordinator_cases(projection)
    decisions: list[dict[str, Any]] = []
    existing_tasks = {str(case.get("task") or "") for case in cases}
    existing_prs = {int(case["pr"]) for case in cases if case.get("pr") is not None}

    for lifecycle in projection.get("resolved_lifecycle") or []:
        if not isinstance(lifecycle, Mapping):
            continue
        pr = int(lifecycle.get("pr") or 0)
        if not pr or pr in existing_prs:
            continue
        state = str(lifecycle.get("state") or "")
        phase = str(lifecycle.get("phase") or "")
        reason_class = ""
        next_action = ""
        if state == "INTEGRATION_READY":
            reason_class = "WORK_READY_TO_SHIP"
            next_action = "decide whether to route the exact reviewed candidate to Integration now"
        elif phase in {"READY_FOR_REVIEW", "REVIEW_IN_PROGRESS"}:
            reason_class = "WORK_READY_TO_ADVANCE"
            next_action = "check the exact Review state and route the next existing lifecycle step"
        elif state == "BLOCKED_EXTERNAL":
            reason_class = "ACTIVE_DELIVERY_BLOCKER"
            next_action = "track the exact external blocker owner and wake condition"
        elif state == "CONTRADICTION" or lifecycle.get("truth") == "CONTRADICTION":
            reason_class = "AUTHORITATIVE_STATE_CONTRADICTION"
            next_action = "hold unsafe coordination and route reconciliation to the exact authority owner"
        if reason_class:
            cases.append(_case(
                repository=repository,
                reason_class=reason_class,
                pr=pr,
                head=str(lifecycle.get("head") or "") or None,
                evidence={
                    "state": state,
                    "phase": phase,
                    "truth": lifecycle.get("truth"),
                    "task_gids": list(lifecycle.get("task_gids") or []),
                    "conflicts": list(lifecycle.get("conflicts") or []),
                },
                next_action=next_action,
            ))
            existing_prs.add(pr)

    for raw_task in projection.get("tasks") or []:
        if not isinstance(raw_task, Mapping) or raw_task.get("error") or raw_task.get("completed"):
            continue
        task_gid = str(raw_task.get("gid") or "")
        execution = raw_task.get("execution") if isinstance(raw_task.get("execution"), Mapping) else {}
        priority = str(execution.get("priority") or "").upper()
        sections = _membership_sections(raw_task)
        if task_gid not in existing_tasks and priority in {"P-CRITICAL", "P0"}:
            cases.append(_case(
                repository=repository,
                reason_class="CRITICAL_WORK_ATTENTION_REQUIRED",
                task=task_gid or None,
                evidence={"priority": priority, "task_name": raw_task.get("name")},
                next_action="assess the exact critical work state and decide the next coordination action",
            ))
            existing_tasks.add(task_gid)
        elif task_gid not in existing_tasks and sections & ATTENTION_SECTIONS:
            cases.append(_case(
                repository=repository,
                reason_class="STUCK_WORK_ATTENTION_REQUIRED",
                task=task_gid or None,
                evidence={
                    "sections": sorted(sections & ATTENTION_SECTIONS),
                    "task_name": raw_task.get("name"),
                },
                next_action="assess the exact stuck work state and decide the next coordination action",
            ))
            existing_tasks.add(task_gid)
        elif task_gid not in existing_tasks and sections & READY_SECTIONS:
            cases.append(_case(
                repository=repository,
                reason_class="WORK_READY_TO_SCHEDULE",
                task=task_gid or None,
                evidence={"sections": sorted(sections & READY_SECTIONS), "task_name": raw_task.get("name")},
                next_action="decide whether the exact ready work should enter the current delivery wave",
            ))
            existing_tasks.add(task_gid)
        elif task_gid not in existing_tasks and sections & BLOCKED_SECTIONS:
            cases.append(_case(
                repository=repository,
                reason_class="ACTIVE_DELIVERY_BLOCKER",
                task=task_gid or None,
                evidence={"sections": sorted(sections & BLOCKED_SECTIONS), "task_name": raw_task.get("name")},
                next_action="track the exact dependency owner and wake condition",
            ))
            existing_tasks.add(task_gid)

    return cases, decisions


def _noon_cases(projection: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repository = str(projection.get("repository") or "")
    scope = projection.get("task_scope") if isinstance(projection.get("task_scope"), Mapping) else {}
    observed_projects = {str(value) for value in scope.get("projects") or []}
    required_projects = {FRICTION_PROJECT, DEBT_PROJECT, DEVELOPMENT_WORKFLOW_PROJECT}
    if scope.get("status") != "COMPLETE" or not required_projects.issubset(observed_projects):
        return [], [{"result": "suppressed", "reason": "authoritative_noon_evidence_unavailable"}]

    cases: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for task in projection.get("tasks") or []:
        if not isinstance(task, Mapping) or task.get("error") or task.get("completed"):
            continue
        projects = _membership_projects(task)
        sections = _membership_sections(task)
        task_gid = str(task.get("gid") or "")
        name = str(task.get("name") or "")
        if projects & {FRICTION_PROJECT, DEBT_PROJECT} and "inbox" in sections:
            cases.append(_case(
                repository=repository,
                reason_class="DEVELOPMENT_WORKFLOW_TRIAGE_DUE",
                task=task_gid or None,
                evidence={"projects": sorted(projects), "sections": sorted(sections), "task_name": name},
                next_action="route the due semantic triage to Development Workflow and later verify completion",
            ))
        elif DEVELOPMENT_WORKFLOW_PROJECT in projects and "audit" in name.lower() and sections & {"ready", "needs processing"}:
            cases.append(_case(
                repository=repository,
                reason_class="AUDIT_SCHEDULING_DUE",
                task=task_gid or None,
                evidence={"projects": sorted(projects), "sections": sorted(sections), "task_name": name},
                next_action="schedule or route the due audit under its existing cadence and ownership rules",
            ))
    return cases, decisions


def consume_projection(
    projection: Mapping[str, Any],
    *,
    duty_id: str,
    policy_generation: str = POLICY_GENERATION,
) -> CoordinatorProjection:
    """Build a deterministic frontier without admitting or starting a wake."""
    duty = duty_for(duty_id, role="Coordinator")
    if duty_id == "coordinator.hourly-frontier":
        raw_cases, decisions = _hourly_cases(projection)
    else:
        raw_cases, decisions = _noon_cases(projection)

    by_version: dict[str, dict[str, Any]] = {}
    for case in raw_cases:
        version = actionable_version(case, policy_generation=policy_generation)
        by_version.setdefault(version, case)
    for version, case in sorted(by_version.items()):
        decisions.append({
            "result": "actionable",
            "reason": "coordinator_owned_v4_actionable_version",
            "actionable_version": version,
            "reason_class": case.get("reason_class"),
            "task": case.get("task"),
            "pr": case.get("pr"),
        })

    cases = tuple(by_version[key] for key in sorted(by_version))
    semantic_frontier = [{
        "actionable_version": version,
        "reason_class": by_version[version].get("reason_class"),
        "task": by_version[version].get("task"),
        "pr": by_version[version].get("pr"),
    } for version in sorted(by_version)]
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": _now(),
        "duty": duty.__dict__,
        "observe_only": True,
        "wake_enabled": False,
        "wake_identity": "Lifecycle V4 actionable_version",
        "wake_policy_generation": policy_generation,
        "frontier_digest": _digest(semantic_frontier),
        "frontier_digest_is_wake_identity": False,
        "authoritative_read": {
            "projection_schema": projection.get("schema"),
            "repository": projection.get("repository"),
            "reconciled_at": projection.get("reconciled_at"),
            "task_scope": dict(projection.get("task_scope") or {}),
            "state_drift": list(projection.get("state_drift") or []),
        },
        "counts": {
            "actionable": len(cases),
            "suppressed": sum(1 for value in decisions if value["result"] == "suppressed"),
            "model_turns_started": 0,
        },
        "decisions": decisions,
        "cases": list(cases),
    }
    return CoordinatorProjection(cases, report)
