#!/usr/bin/env python3
"""Observe-only Integrator adapter over the authoritative lifecycle projection.

This module consumes canonical CI ownership/fingerprint output. It does not
classify failures, create owners, admit wakes, or define a second identity.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

from ci_failure_fingerprint import FingerprintError, validate_fingerprint
from pr_lifecycle_v4 import POLICY_GENERATION, actionable_version


REPORT_SCHEMA = "dish-integrator-observe-report-v1"
AUDIT_SCHEMA = "dish-integrator-audit-v1"
CI_CLASSES = frozenset({
    "PR_OWNED",
    "LIKELY_NON_PR_OWNED",
    "PROVEN_CURRENT_MAIN",
    "INFRASTRUCTURE",
    "AMBIGUOUS",
})
CI_REASON_CLASSES = frozenset({
    "CI_OWNERSHIP_AMBIGUOUS",
    "CI_INFRASTRUCTURE_OR_NETWORK",
    "NIGHTLY_CI_UNRESOLVED",
})
INTEGRATOR_CI_REASON_CLASSES = CI_REASON_CLASSES | {"CI_SLOW"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(dict(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _pr_index(projection: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for value in projection.get("pull_requests") or []:
        if not isinstance(value, Mapping):
            continue
        try:
            number = int(value.get("number"))
        except (TypeError, ValueError):
            continue
        result[number] = value
    return result


def _integrator_cases(projection: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    v3 = projection.get("v3") if isinstance(projection.get("v3"), Mapping) else {}
    integrator = v3.get("integrator") if isinstance(v3.get("integrator"), Mapping) else {}
    return [
        value for value in integrator.get("active_cases") or []
        if isinstance(value, Mapping) and value.get("next_owner") == "Integrator"
    ]


def _canonical_ci(gate: Mapping[str, Any]) -> dict[str, Any]:
    fingerprint = str(gate.get("failure_causal_fingerprint") or "").strip().lower()
    if fingerprint:
        try:
            validate_fingerprint(fingerprint)
        except FingerprintError as exc:
            raise ValueError("canonical CI causal fingerprint is invalid") from exc
    result = {
        "classification": str(gate.get("failure_ownership") or ""),
        "candidate_disposition": gate.get("candidate_disposition"),
        "causal_fingerprint": fingerprint or None,
        "causal_identity": gate.get("failure_causal_identity"),
        "repair_owner_task": gate.get("repair_owner_task"),
        "repair_owner_active": bool(gate.get("repair_owner_active")),
        "failure_main_sha": gate.get("failure_main_sha"),
        "evidence_generation": gate.get("evidence_generation"),
        "ownership_evidence": gate.get("failure_ownership_evidence"),
        "required_check": gate.get("required_check") or gate.get("required_status_context"),
        "workflow_run_id": gate.get("required_workflow_run_id"),
        "workflow_run_attempt": gate.get("required_workflow_run_attempt"),
        "raw_gate_outcome": gate.get("raw_gate_outcome") or "FAILED",
    }
    return {key: value for key, value in result.items() if value is not None and value != ""}


@dataclass(frozen=True)
class IntegratorProjection:
    actionable_cases: tuple[dict[str, Any], ...]
    report: dict[str, Any]


def consume_projection(
    projection: Mapping[str, Any], *, policy_generation: str = POLICY_GENERATION
) -> IntegratorProjection:
    """Return exact V4 cases plus a reconstructable observe-only report."""
    prs = _pr_index(projection)
    actionable: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for raw_case in _integrator_cases(projection):
        case = dict(raw_case)
        reason_class = str(case.get("reason_class") or "")
        evidence = dict(case.get("evidence") or {})
        decision: dict[str, Any] = {
            "case_key": case.get("case_key"),
            "reason_class": reason_class,
            "pr": case.get("pr"),
            "next_owner": "Integrator",
        }
        if reason_class not in INTEGRATOR_CI_REASON_CLASSES:
            decision.update(result="suppressed", reason="outside_ci_reliability_scope")
            decisions.append(decision)
            continue
        if reason_class in CI_REASON_CLASSES:
            try:
                pr_number = int(case.get("pr"))
            except (TypeError, ValueError):
                pr_number = -1
            pr = prs.get(pr_number)
            gate = pr.get("gate") if isinstance(pr, Mapping) and isinstance(pr.get("gate"), Mapping) else None
            if gate is None and isinstance(evidence.get("canonical_ci"), Mapping):
                gate = evidence["canonical_ci"]
            if gate is None:
                decision.update(result="suppressed", reason="authoritative_ci_projection_unavailable")
                decisions.append(decision)
                continue
            try:
                canonical = _canonical_ci(gate)
            except ValueError as exc:
                decision.update(result="suppressed", reason=str(exc))
                decisions.append(decision)
                continue
            classification = str(canonical.get("classification") or "")
            if classification not in CI_CLASSES:
                decision.update(result="suppressed", reason="canonical_ci_classification_unavailable")
                decisions.append(decision)
                continue
            case_classification = str(evidence.get("failure_ownership") or classification)
            if case_classification != classification:
                decision.update(result="suppressed", reason="canonical_ci_projection_contradiction")
                decisions.append(decision)
                continue
            evidence["canonical_ci"] = canonical
            case["evidence"] = evidence

        version = actionable_version(case, policy_generation=policy_generation)
        actionable.append(case)
        decision.update(
            result="actionable",
            reason="integrator_owned_v4_actionable_version",
            actionable_version=version,
            canonical_ci=canonical if reason_class in CI_REASON_CLASSES else None,
        )
        decisions.append(decision)

    # Settled canonical ownership is absent from V3 attention by design. Record
    # that deterministic zero-turn decision so it remains reviewable.
    active_prs: set[int] = set()
    for case in actionable:
        try:
            active_prs.add(int(case.get("pr")))
        except (TypeError, ValueError):
            continue
    for number, pr in sorted(prs.items()):
        gate = pr.get("gate") if isinstance(pr.get("gate"), Mapping) else {}
        if gate.get("diagnosis") != "FAILED_REQUIRED_CI" or number in active_prs:
            continue
        classification = str(gate.get("failure_ownership") or "")
        disposition = str(gate.get("candidate_disposition") or "")
        owner_active = bool(gate.get("repair_owner_active"))
        settled = (
            classification == "LIKELY_NON_PR_OWNED"
            and disposition == "NON_BLOCKING_LIKELY_UNRELATED"
            and owner_active
        ) or (
            classification == "PROVEN_CURRENT_MAIN"
            and disposition == "MERGEABLE_WITH_BASELINE_DEBT"
            and owner_active
        )
        if settled:
            decisions.append({
                "reason_class": "CI_SETTLED_WITH_REPAIR_OWNER",
                "pr": number,
                "next_owner": gate.get("repair_owner_task"),
                "result": "suppressed",
                "reason": "canonical_ci_owner_already_active",
                "classification": classification,
                "causal_fingerprint": gate.get("failure_causal_fingerprint"),
            })

    counts = {
        "actionable": sum(1 for value in decisions if value["result"] == "actionable"),
        "suppressed": sum(1 for value in decisions if value["result"] == "suppressed"),
    }
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": _now(),
        "wake_identity": "Lifecycle V4 actionable_version",
        "wake_policy_generation": policy_generation,
        "observe_only": True,
        "counts": counts,
        "decisions": decisions,
    }
    return IntegratorProjection(tuple(actionable), report)


class IntegratorAudit:
    """Small rotated NDJSON evidence log plus latest deterministic report."""

    def __init__(self, path: Path, *, report_path: Path, max_bytes: int = 2_000_000):
        self.path = path
        self.report_path = report_path
        self.max_bytes = max(64_000, int(max_bytes))
        self._lock = threading.Lock()

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.with_suffix(self.path.suffix + ".lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_unlocked(self, record: Mapping[str, Any]) -> None:
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
            rotated = self.path.with_suffix(self.path.suffix + ".1")
            os.replace(self.path, rotated)
        with self.path.open("ab") as handle:
            handle.write(encoded)

    def write(self, event: str, **values: Any) -> None:
        record = {"schema": AUDIT_SCHEMA, "at": _now(), "event": event, **values}
        with self._locked():
            self._write_unlocked(record)

    def publish_report(self, report: Mapping[str, Any]) -> None:
        with self._locked():
            _atomic_json(self.report_path, report)
            self._write_unlocked({
                "schema": AUDIT_SCHEMA,
                "at": _now(),
                "event": "projection_consumed",
                "counts": dict(report.get("counts") or {}),
                "decisions": list(report.get("decisions") or []),
                "model_turns_started": 0,
            })

    def records(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        with self._locked():
            for path in (self.path.with_suffix(self.path.suffix + ".1"), self.path):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except FileNotFoundError:
                    continue
                for line in lines:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        values.append(value)
        return values


def model_outcome_for_wake(read: Mapping[str, Any], wake_id: str) -> dict[str, Any] | None:
    """Extract the persisted final structured proposal for one completed wake."""
    thread = read.get("thread") if isinstance(read.get("thread"), Mapping) else {}
    for turn in thread.get("turns") or []:
        if not isinstance(turn, Mapping):
            continue
        items = [value for value in turn.get("items") or [] if isinstance(value, Mapping)]
        matching = any(
            str(item.get("type") or "") in {"userMessage", "user_message"}
            and str(item.get("clientId") or "") == wake_id
            for item in items
        )
        if not matching:
            continue
        messages = [
            str(item.get("text") or "")
            for item in items
            if str(item.get("type") or "") in {"agentMessage", "agent_message"}
            and str(item.get("phase") or "final_answer") == "final_answer"
        ]
        if not messages:
            return {"valid": False, "reason": "completed turn has no final agent message"}
        try:
            proposal = json.loads(messages[-1])
        except json.JSONDecodeError:
            return {"valid": False, "reason": "final agent message is not JSON"}
        if not isinstance(proposal, Mapping):
            return {"valid": False, "reason": "final agent message is not a JSON object"}
        return {"valid": True, "proposal": dict(proposal), "turn_id": turn.get("id")}
    return None
