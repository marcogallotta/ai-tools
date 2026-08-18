"""Lifecycle V3 inactive executor and derived attention projection.

This module is policy/projection only.  It does not own a merge path, scheduler,
queue, database, or model launcher.  The existing PR lifecycle inspector and
Integration V1-A fence remain the authoritative mechanical gate/writer while V3
is shadowed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Iterable, Mapping

V3_SCHEMA = "dish-pr-lifecycle-v3-v1"
V3_MODE = "SHADOW_INACTIVE"
LEGACY_WRITER = "integration-v1a-local-fenced"
CANDIDATE_WRITER = "v3-deterministic"
DEFAULT_CI_SLOW_SECONDS = 30 * 60

_INTEGRATOR_REASON_CLASSES = {
    "CI_SLOW",
    "CI_RED_CURRENT_MAIN_OR_EXTERNAL",
    "CI_INFRASTRUCTURE_OR_NETWORK",
    "CI_OWNERSHIP_AMBIGUOUS",
    "MERGE_CONFLICT_OR_BASE_RECONCILIATION_REQUIRED",
    "AUTHORITY_CONTRADICTION",
    "INTEGRATION_READBACK_UNCERTAIN",
    "RECURRING_BUILD_HEALTH_PATTERN",
}


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ci_slow_seconds() -> int:
    """Local attention tuning only; this value never changes merge eligibility."""
    raw = str(os.getenv("DISH_V3_CI_SLOW_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_CI_SLOW_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CI_SLOW_SECONDS
    return max(60, min(value, 24 * 60 * 60))


def _task_index(tasks: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        str(task.get("gid")): task
        for task in tasks
        if isinstance(task, Mapping) and task.get("gid")
    }


def hold_for_pr(
    pr: Mapping[str, Any],
    tasks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    task_ids = [str(value) for value in pr.get("task_ids") or [] if value]
    if not task_ids:
        return {
            "state": "UNKNOWN",
            "reason": "owning task identity is unavailable",
            "tasks": [],
        }

    values: list[dict[str, Any]] = []
    for gid in task_ids:
        task = tasks.get(gid)
        if task is None or task.get("error"):
            return {
                "state": "UNKNOWN",
                "reason": f"owning task {gid} could not be read",
                "tasks": task_ids,
            }
        execution = task.get("execution") if isinstance(task.get("execution"), Mapping) else {}
        hold = (
            execution.get("source_landing_hold")
            if isinstance(execution.get("source_landing_hold"), Mapping)
            else None
        )
        if hold is None:
            return {
                "state": "UNKNOWN",
                "reason": f"owning task {gid} lacks explicit hold evaluation",
                "tasks": task_ids,
            }
        values.append({"task": gid, **dict(hold)})

    contradictions = [value for value in values if value.get("state") == "CONTRADICTION"]
    if contradictions:
        return {
            "state": "CONTRADICTION",
            "reason": "human-hold lineage is malformed or contradictory",
            "tasks": task_ids,
            "evidence": contradictions,
        }
    held = [value for value in values if value.get("state") == "HELD"]
    if held:
        return {
            "state": "HELD",
            "reason": "explicit durable human source-landing hold is active",
            "tasks": task_ids,
            "evidence": held,
        }
    if all(value.get("state") == "CLEAR" for value in values):
        return {
            "state": "CLEAR",
            "reason": "no active explicit durable human source-landing hold",
            "tasks": task_ids,
            "evidence": values,
        }
    return {
        "state": "UNKNOWN",
        "reason": "human-hold authority could not be proven",
        "tasks": task_ids,
        "evidence": values,
    }


def inactive_executor_decision(
    pr: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    hold: Mapping[str, Any],
) -> dict[str, Any]:
    """Plan the V3 happy path using the existing Integration-ready boundary.

    The current inspector already proves open/not-draft, exact reviewed head,
    Review MERGE, exact-head CI/certification, pre-integration evidence,
    mergeability/order, and current local Integration capability.  Re-encoding
    those predicates here would create a competing gate engine, so V3 consumes
    that authoritative derived boundary and adds only the V3 human-hold rule.
    """
    lifecycle_state = str(pr.get("state") or "")
    head = str(pr.get("head") or "")
    reviewed_head = str(pr.get("reviewed_head") or "")
    review_verdict = str(pr.get("review_verdict") or "")
    gate = pr.get("gate") if isinstance(pr.get("gate"), Mapping) else {}

    if lifecycle_state == "merged":
        decision = "NO_ACTION_ALREADY_MERGED"
        reason = "authoritative GitHub lifecycle already reports merged"
    elif lifecycle_state == "merging_integration_in_progress":
        decision = "OBSERVE_EXISTING_WRITER"
        reason = "existing V1-A Integration writer owns the exact PR/head fence"
    elif lifecycle_state != "integration_ready":
        decision = "WAIT_CURRENT_LIFECYCLE"
        reason = "existing lifecycle has not proved the Integration-ready boundary"
    elif source.get("state") != "NOT_LANDED":
        decision = "BLOCK_SOURCE_IDENTITY"
        reason = "candidate is not proven unlanded on its intended source lineage"
    elif not head or review_verdict != "MERGE" or reviewed_head != head:
        decision = "BLOCK_EXACT_HEAD_REVIEW"
        reason = "existing Integration-ready identity no longer matches exact-head Review"
    elif hold.get("state") == "HELD":
        decision = "BLOCK_HUMAN_HOLD"
        reason = "explicit durable human source-landing hold is active"
    elif hold.get("state") in {"UNKNOWN", "CONTRADICTION"}:
        decision = "BLOCK_HOLD_AUTHORITY"
        reason = "human-hold authority is unknown or contradictory"
    else:
        decision = "WOULD_EXECUTE_EXISTING_INTEGRATION"
        reason = (
            "existing lifecycle proved Integration-ready and V3 hold evaluation is clear; "
            "inactive V3 would reuse the current fenced Integration machinery"
        )

    return {
        "pr": int(pr["number"]),
        "head": head or None,
        "decision": decision,
        "reason": reason,
        "admission_basis": "existing-integration-ready-state",
        "current_lifecycle_state": lifecycle_state,
        "current_review_verdict": review_verdict or None,
        "current_reviewed_head": reviewed_head or None,
        "current_gate_diagnosis": gate.get("diagnosis"),
        "source_state": source.get("state"),
        "human_hold": dict(hold),
        "execution_adapter": LEGACY_WRITER,
        "write_authority": False,
        "mutation_permitted": False,
    }


def _case(
    *,
    repository: str,
    reason_class: str,
    pr: Mapping[str, Any] | None,
    task: str | None,
    evidence: Mapping[str, Any],
    next_owner: str,
    next_action: str,
    observed_at: datetime,
    first_seen: datetime | None = None,
) -> dict[str, Any]:
    pr_number = int(pr["number"]) if pr is not None else None
    head = str(pr.get("head") or "") if pr is not None else ""
    task_ids = [str(value) for value in (pr.get("task_ids") or [])] if pr is not None else []
    identity = {
        "repository": repository,
        "reason_class": reason_class,
        "pr": pr_number,
        "task": task or (task_ids[0] if len(task_ids) == 1 else None),
        "head": head or None,
        "evidence": dict(evidence),
    }
    fingerprint = hashlib.sha256(_canonical(identity).encode()).hexdigest()
    key_material = {
        key: identity[key]
        for key in ("repository", "reason_class", "pr", "task", "head")
    }
    key_material["fingerprint"] = fingerprint
    case_key = hashlib.sha256(_canonical(key_material).encode()).hexdigest()[:24]
    started = (first_seen or observed_at).astimezone(timezone.utc)
    return {
        "case_key": case_key,
        "reason_class": reason_class,
        "repository": repository,
        "pr": pr_number,
        "task": identity["task"],
        "head": head or None,
        "reviewed_head": pr.get("reviewed_head") if pr is not None else None,
        "review_verdict": pr.get("review_verdict") if pr is not None else None,
        "first_seen": started.isoformat(),
        "last_changed": observed_at.astimezone(timezone.utc).isoformat(),
        "evidence_fingerprint": fingerprint,
        "evidence": dict(evidence),
        "next_owner": next_owner,
        "next_action": next_action,
    }


def _gate_time(gate: Mapping[str, Any], fallback: datetime) -> datetime:
    for key in (
        "required_workflow_run_started_at",
        "required_workflow_run_updated_at",
        "workflow_run_started_at",
        "started_at",
    ):
        parsed = _instant(gate.get(key))
        if parsed is not None:
            return parsed
    return fallback


def attention_cases(
    prs: Iterable[Mapping[str, Any]],
    *,
    tasks: Iterable[Mapping[str, Any]],
    sources: Mapping[str, Any],
    repository: str,
    controller: Mapping[str, Any],
    generated_at: datetime,
    slow_seconds: int | None = None,
) -> list[dict[str, Any]]:
    threshold = ci_slow_seconds() if slow_seconds is None else max(60, int(slow_seconds))
    task_map = _task_index(tasks)
    cases: list[dict[str, Any]] = []

    for pr in prs:
        source = dict(
            sources.get(str(pr["number"]))
            or sources.get(pr["number"])
            or {}
        )
        hold = hold_for_pr(pr, task_map)
        gate = pr.get("gate") if isinstance(pr.get("gate"), Mapping) else {}
        diagnosis = str(gate.get("diagnosis") or "")
        ownership = str(gate.get("failure_ownership") or "")
        lifecycle_state = str(pr.get("state") or "")
        residual = str(pr.get("residual_reason") or "")
        evidence_time = _gate_time(gate, generated_at)

        if diagnosis == "PENDING" and (generated_at - evidence_time).total_seconds() >= threshold:
            cases.append(_case(
                repository=repository,
                reason_class="CI_SLOW",
                pr=pr,
                task=None,
                evidence={
                    "diagnosis": diagnosis,
                    "gate_started_at": evidence_time.isoformat(),
                    "attention_threshold_seconds": threshold,
                    "required_status_context": gate.get("required_status_context"),
                },
                next_owner="Integrator",
                next_action="diagnose why exact-head CI remains slow; do not change merge authority",
                observed_at=generated_at,
                first_seen=evidence_time,
            ))

        if diagnosis == "FAILED_REQUIRED_CI":
            if ownership == "PR_OWNED":
                cases.append(_case(
                    repository=repository,
                    reason_class="CI_RED_PR_OWNED",
                    pr=pr,
                    task=None,
                    evidence={
                        "failure_ownership": ownership,
                        "ownership_evidence": gate.get("failure_ownership_evidence"),
                        "required_status_context": gate.get("required_status_context"),
                    },
                    next_owner="Implementation",
                    next_action="return exact PR/head failure evidence through the existing fix route",
                    observed_at=generated_at,
                    first_seen=evidence_time,
                ))
            elif ownership == "PROVEN_CURRENT_MAIN":
                cases.append(_case(
                    repository=repository,
                    reason_class="CI_RED_CURRENT_MAIN_OR_EXTERNAL",
                    pr=pr,
                    task=None,
                    evidence={
                        "failure_ownership": ownership,
                        "ownership_evidence": gate.get("failure_ownership_evidence"),
                        "required_status_context": gate.get("required_status_context"),
                    },
                    next_owner="Coordinator",
                    next_action="schedule the proven external/current-main repair; do not mutate the candidate",
                    observed_at=generated_at,
                    first_seen=evidence_time,
                ))
            elif ownership == "INFRASTRUCTURE":
                cases.append(_case(
                    repository=repository,
                    reason_class="CI_INFRASTRUCTURE_OR_NETWORK",
                    pr=pr,
                    task=None,
                    evidence={
                        "failure_ownership": ownership,
                        "ownership_evidence": gate.get("failure_ownership_evidence"),
                    },
                    next_owner="Integrator",
                    next_action="diagnose infrastructure failure and preserve the candidate unchanged",
                    observed_at=generated_at,
                    first_seen=evidence_time,
                ))
            else:
                cases.append(_case(
                    repository=repository,
                    reason_class="CI_OWNERSHIP_AMBIGUOUS",
                    pr=pr,
                    task=None,
                    evidence={
                        "failure_ownership": ownership or "AMBIGUOUS",
                        "ownership_evidence": gate.get("failure_ownership_evidence"),
                    },
                    next_owner="Integrator",
                    next_action="diagnose CI ownership; no semantic mutation until ownership is proven",
                    observed_at=generated_at,
                    first_seen=evidence_time,
                ))
        elif diagnosis == "INFRASTRUCTURE_ERROR":
            cases.append(_case(
                repository=repository,
                reason_class="CI_INFRASTRUCTURE_OR_NETWORK",
                pr=pr,
                task=None,
                evidence={"diagnosis": diagnosis, "reason": gate.get("reason")},
                next_owner="Integrator",
                next_action="diagnose transport/infrastructure evidence failure; keep candidate unchanged",
                observed_at=generated_at,
                first_seen=evidence_time,
            ))

        lowered = residual.lower()
        if (
            lifecycle_state == "review_passed_evaluating_gates"
            and any(token in lowered for token in ("merge conflict", "mergeability", "base reconciliation"))
        ):
            cases.append(_case(
                repository=repository,
                reason_class="MERGE_CONFLICT_OR_BASE_RECONCILIATION_REQUIRED",
                pr=pr,
                task=None,
                evidence={"residual_reason": residual},
                next_owner="Integrator",
                next_action=(
                    "diagnose whether reconciliation is uniquely mechanical; semantic choices return to Implementation"
                ),
                observed_at=generated_at,
            ))
        if "readback" in lowered and lifecycle_state != "merged":
            cases.append(_case(
                repository=repository,
                reason_class="INTEGRATION_READBACK_UNCERTAIN",
                pr=pr,
                task=None,
                evidence={"residual_reason": residual},
                next_owner="Integrator",
                next_action="reconcile authoritative GitHub state before any replay",
                observed_at=generated_at,
            ))

        if hold.get("state") == "CONTRADICTION":
            cases.append(_case(
                repository=repository,
                reason_class="AUTHORITY_CONTRADICTION",
                pr=pr,
                task=None,
                evidence={"human_hold": dict(hold)},
                next_owner="Marco",
                next_action="resolve the contradictory explicit hold/release lineage",
                observed_at=generated_at,
            ))
        elif hold.get("state") == "HELD":
            cases.append(_case(
                repository=repository,
                reason_class="HUMAN_HOLD",
                pr=pr,
                task=None,
                evidence={"human_hold": dict(hold)},
                next_owner="Marco",
                next_action="leave source landing paused until an explicit durable human release is recorded",
                observed_at=generated_at,
            ))

        if lifecycle_state == "merged" and pr.get("post_merge_gates"):
            cases.append(_case(
                repository=repository,
                reason_class="POST_MERGE_ACTION_REQUIRED",
                pr=pr,
                task=None,
                evidence={"post_merge_gates": list(pr.get("post_merge_gates") or [])},
                next_owner="Coordinator",
                next_action="schedule/track residual post-merge acceptance work",
                observed_at=generated_at,
            ))

    for task in tasks:
        if not isinstance(task, Mapping) or task.get("error"):
            continue
        execution = task.get("execution") if isinstance(task.get("execution"), Mapping) else {}
        stale_kind = str(execution.get("stale_kind") or "")
        if stale_kind not in {"WORKER_ACCEPTANCE_STALE", "WORKER_EXECUTION_STALE"}:
            continue
        timestamp = _instant(execution.get("timestamp")) or generated_at
        cases.append(_case(
            repository=repository,
            reason_class=stale_kind,
            pr=None,
            task=str(task.get("gid") or "") or None,
            evidence={
                "execution_state": execution.get("state"),
                "timestamp": execution.get("timestamp"),
            },
            next_owner="Coordinator",
            next_action="re-read exact attempt/claim evidence before any recovery or replacement dispatch",
            observed_at=generated_at,
            first_seen=timestamp,
        ))

    controller_status = str(controller.get("status") or "").lower()
    if controller_status in {"offline", "degraded"}:
        offline_since = _instant(controller.get("offline_since")) or generated_at
        cases.append(_case(
            repository=repository,
            reason_class="CI_INFRASTRUCTURE_OR_NETWORK",
            pr=None,
            task=None,
            evidence={
                "controller_status": controller_status,
                "last_error": controller.get("last_error"),
                "next_retry_seconds": controller.get("next_retry_seconds"),
            },
            next_owner="Integrator",
            next_action="remain degraded, back off, and rebuild authoritative state after connectivity returns",
            observed_at=generated_at,
            first_seen=offline_since,
        ))

    deduped: dict[str, dict[str, Any]] = {}
    for case in cases:
        deduped[case["case_key"]] = case
    return sorted(
        deduped.values(),
        key=lambda item: (str(item["reason_class"]), int(item.get("pr") or 0), str(item["case_key"])),
    )


def build_v3_projection(
    prs: Iterable[Mapping[str, Any]],
    *,
    tasks: Iterable[Mapping[str, Any]],
    source_observation: Mapping[str, Any],
    repository: str,
    controller: Mapping[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    pr_values = [dict(pr) for pr in prs]
    task_values = [dict(task) for task in tasks]
    task_map = _task_index(task_values)
    sources = dict(source_observation.get("pull_requests") or {})

    executor = []
    for pr in pr_values:
        source = dict(
            sources.get(str(pr["number"]))
            or sources.get(pr["number"])
            or {}
        )
        executor.append(inactive_executor_decision(
            pr,
            source=source,
            hold=hold_for_pr(pr, task_map),
        ))

    cases = attention_cases(
        pr_values,
        tasks=task_values,
        sources=sources,
        repository=repository,
        controller=controller,
        generated_at=generated_at,
    )
    integrator_cases = [
        case for case in cases if case["reason_class"] in _INTEGRATOR_REASON_CLASSES
    ]
    coordinator_outputs = [
        {
            "case_key": case["case_key"],
            "reason_class": case["reason_class"],
            "pr": case.get("pr"),
            "task": case.get("task"),
            "head": case.get("head"),
            "next_owner": case["next_owner"],
            "next_action": case["next_action"],
        }
        for case in cases
        if case["next_owner"] in {"Coordinator", "Implementation"}
    ]

    provider = str(controller.get("integrator_provider") or "").strip() or None
    return {
        "schema": V3_SCHEMA,
        "mode": V3_MODE,
        "activation_authorized": False,
        "write_authority": False,
        "authoritative_landing_path": LEGACY_WRITER,
        "writer": {
            "active": LEGACY_WRITER,
            "candidate": CANDIDATE_WRITER,
            "candidate_enabled": False,
            "single_writer": True,
            "cutover_authorized": False,
            "rollback": "explicit-reconcile-then-switch-writer",
        },
        "executor": {
            "mode": "INACTIVE",
            "mutation_permitted": False,
            "reuses": "existing Integration-ready inspector + local Integration fence/readback",
            "decisions": executor,
        },
        "attention": {
            "authoritative_queue": False,
            "recomputed_from_live_evidence": True,
            "dedupe": "best-effort deterministic case key",
            "ci_slow_threshold_seconds": ci_slow_seconds(),
            "cases": cases,
        },
        "integrator": {
            "bridge_mode": "INACTIVE_PACKET_ONLY",
            "provider": provider,
            "provider_selection": "local-tunable",
            "launches_hidden_model": False,
            "scheduler_authority": False,
            "review_authority": False,
            "semantic_implementation_authority": False,
            "integration_authority": False,
            "active_cases": integrator_cases,
        },
        "coordinator_outputs": coordinator_outputs,
    }
