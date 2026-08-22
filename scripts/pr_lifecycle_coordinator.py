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

from pr_lifecycle_v4 import POLICY_GENERATION, actionable_version


REPORT_SCHEMA = "dish-coordinator-observe-report-v1"
FRONTIER_SCHEMA = "dish-coordinator-frontier-v1"
AUDIT_SCHEMA = "dish-coordinator-audit-v1"
AUTHORITATIVE_RESIDUAL_KINDS = frozenset({
    "explicit_human_decision",
    "standing_repository_policy",
    "accepted_task_design_obligation",
})
ATTENTION_SECTIONS = frozenset({
    "needs research",
    "needs agentic review",
    "needs human review",
})


@dataclass(frozen=True)
class DutySpec:
    duty_id: str
    role: str
    schedule: str
    handler: str
    output_schema: str
    observe_only: bool = True


DUTIES = {
    "coordinator.hourly-frontier": DutySpec(
        duty_id="coordinator.hourly-frontier",
        role="Coordinator",
        schedule="hourly",
        handler="coordinator_hourly_frontier",
        output_schema=FRONTIER_SCHEMA,
    ),
    "coordinator.noon-hygiene": DutySpec(
        duty_id="coordinator.noon-hygiene",
        role="Coordinator",
        schedule="12:00 Europe/Rome",
        handler="coordinator_noon_hygiene",
        output_schema=FRONTIER_SCHEMA,
    ),
}


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
        "counts": dict(report.get("counts") or {}),
        "decisions": list(report.get("decisions") or []),
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

        for residual in raw_task.get("residuals") or []:
            if not isinstance(residual, Mapping) or residual.get("state") not in {"active", "deferred"}:
                continue
            provenance = residual.get("provenance") if isinstance(residual.get("provenance"), Mapping) else {}
            if provenance.get("kind") not in AUTHORITATIVE_RESIDUAL_KINDS:
                decisions.append({
                    "result": "suppressed",
                    "reason": "residual_lacks_authoritative_provenance",
                    "task": task_gid,
                    "residual": residual.get("id"),
                })
                continue
            if not residual.get("owner") or not residual.get("wake_condition"):
                decisions.append({
                    "result": "suppressed",
                    "reason": "residual_owner_or_wake_unavailable",
                    "task": task_gid,
                    "residual": residual.get("id"),
                })
                continue
            cases.append(_case(
                repository=repository,
                reason_class="AUTHORITATIVE_RESIDUAL_AT_RISK",
                task=task_gid or None,
                evidence={
                    "residual_id": residual.get("id"),
                    "state": residual.get("state"),
                    "owner": residual.get("owner"),
                    "wake_condition": residual.get("wake_condition"),
                    "provenance": dict(provenance),
                },
                next_action="preserve the residual's exact owner, state and wake condition in the delivery plan",
            ))
    return cases, decisions


def _noon_cases(projection: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repository = str(projection.get("repository") or "")
    evidence = projection.get("coordinator_duty_evidence")
    evidence = evidence.get("coordinator.noon-hygiene") if isinstance(evidence, Mapping) else None
    if not isinstance(evidence, Mapping) or evidence.get("status") != "COMPLETE":
        return [], [{"result": "suppressed", "reason": "authoritative_noon_evidence_unavailable"}]

    cases: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for item in evidence.get("items") or []:
        if not isinstance(item, Mapping) or not item.get("due"):
            continue
        kind = str(item.get("kind") or "")
        source_id = str(item.get("source_id") or "")
        if kind == "audit_due" and source_id:
            cases.append(_case(
                repository=repository,
                reason_class="AUDIT_SCHEDULING_DUE",
                evidence={"source_id": source_id, "cadence": item.get("cadence")},
                next_action="schedule or route the due audit under its existing cadence and ownership rules",
            ))
        elif kind == "development_workflow_hygiene_due" and source_id:
            cases.append(_case(
                repository=repository,
                reason_class="DEVELOPMENT_WORKFLOW_TRIAGE_DUE",
                evidence={"source_id": source_id, "queue": item.get("queue")},
                next_action="route the due semantic triage to Development Workflow and later verify completion",
            ))
        else:
            decisions.append({"result": "suppressed", "reason": "unsupported_or_unidentified_noon_item"})
    return cases, decisions


def consume_projection(
    projection: Mapping[str, Any],
    *,
    duty_id: str,
    policy_generation: str = POLICY_GENERATION,
) -> CoordinatorProjection:
    """Build a deterministic frontier without admitting or starting a wake."""
    if duty_id not in DUTIES:
        raise ValueError(f"unknown Coordinator duty: {duty_id}")
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
        "duty": DUTIES[duty_id].__dict__,
        "observe_only": True,
        "wake_enabled": False,
        "wake_identity": "Lifecycle V4 actionable_version",
        "wake_policy_generation": policy_generation,
        "frontier_digest": _digest(semantic_frontier),
        "frontier_digest_is_wake_identity": False,
        "counts": {
            "actionable": len(cases),
            "suppressed": sum(1 for value in decisions if value["result"] == "suppressed"),
            "model_turns_started": 0,
        },
        "decisions": decisions,
    }
    return CoordinatorProjection(cases, report)
