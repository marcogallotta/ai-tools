#!/usr/bin/env python3
"""Observational lifecycle/operator economics telemetry.

The source envelope is deliberately closed and content-free. Authoritative adapters
translate existing GitHub/Asana/lifecycle facts into that envelope; aggregation and
reporting remain diagnostic only. Telemetry failures never alter lifecycle truth.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "lifecycle-economics-source-event/v1"
UNKNOWN = "UNKNOWN"
PR_FLOW = "pr_flow"
REPOSITORY_HEALTH = "repository_health"
SERIES = {PR_FLOW, REPOSITORY_HEALTH}
USAGE_FIELDS = ("tool_calls", "input_tokens", "output_tokens", "total_tokens")
EXECUTION_FIELDS = ("execution_id", "host", "provider", "model", "config", "snapshot")
OPERATOR_CATEGORIES = {
    "design_risk_product_decision",
    "manual_relay_or_queue_routing",
    "status_reconciliation_or_repair",
    "override_waiver_permission_prompt",
    "integration_merge_controller_babysitting",
    "workflow_incident_firefighting",
}
REVIEW_PHASES = {"dispatched", "blocked", "fix_started", "rereview_requested", "passed"}
TOP_LEVEL_FIELDS = {
    "schema_version", "source", "source_event_id", "observed_at", "series", "repository",
    "task_id", "pr_number", "lineage_id", "generation_id", "replaces_generation_id",
    "head_sha", "attempt_id", "event_type", "execution", "timing", "operator", "review",
    "usage", "outcome", "size",
}
NESTED_FIELDS = {
    "execution": set(EXECUTION_FIELDS),
    "timing": {"stage", "duration_ms", "estimate_ms"},
    "operator": {"required", "category", "duration_ms", "action_id", "gate_id", "override"},
    "review": {"round_id", "review_id", "exact_head_sha", "phase"},
    "usage": set(USAGE_FIELDS) | {"cost"},
    "cost": {"amount", "currency", "unit"},
    "outcome": {"kind", "authoritative", "terminal", "evidence_id"},
    "size": {"files_changed", "additions", "deletions", "lines_changed"},
}
HUMAN_NOTICE_RE = re.compile(
    r"<!--\s*dish-human-notice:v1\s+kind=(?P<kind>[^\s>]+)\s+head=(?P<head>[0-9a-fA-F]{40})\s+key=(?P<key>[^\s>]+)\s*-->"
)
OVERRIDE_RE = re.compile(r"(?im)^\s*GATE WAIVED BY MARCO OVERRIDE:\s*(?P<value>[^\n]+?)\s*$")
CANONICAL_OVERRIDE_GATE_RE = re.compile(
    r"(?im)^\s*GATE WAIVED BY MARCO OVERRIDE:\s*gate=(?P<gate>[A-Za-z0-9][A-Za-z0-9._:/-]{0,127})\s*$"
)
NOTICE_CATEGORY = {
    "terminal-cleanup": "workflow_incident_firefighting",
    "review-dispatch-config": "workflow_incident_firefighting",
    "review-dispatch-error": "workflow_incident_firefighting",
    "local-certification": "manual_relay_or_queue_routing",
    "local-implementation": "manual_relay_or_queue_routing",
}


class TelemetryError(ValueError):
    """A telemetry input is malformed, content-bearing, or non-attributable."""


def _known(value: Any) -> bool:
    return value is not None and value != "" and value != UNKNOWN


def _identity(value: Any) -> Any:
    return value if _known(value) else UNKNOWN


def _bounded_string(value: Any, *, field_name: str, max_length: int = 512, required: bool = False) -> Any:
    if not _known(value):
        if required:
            raise TelemetryError(f"{field_name} must be a non-empty string")
        return UNKNOWN
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or len(value) > max_length or "\x00" in value:
        raise TelemetryError(f"{field_name} must be a bounded non-empty identifier")
    return value


def _nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TelemetryError(f"{field_name} must be a non-negative integer")
    return value


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TelemetryError(f"{field_name} must be an exact decimal value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TelemetryError(f"{field_name} must be an exact decimal value") from exc
    if not result.is_finite() or result < 0:
        raise TelemetryError(f"{field_name} must be a finite non-negative decimal")
    return result


def _closed_mapping(value: Any, *, field_name: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TelemetryError(f"{field_name} must be an object")
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise TelemetryError(f"{field_name} contains non-schema fields: {', '.join(unknown)}")
    return dict(value)


def _normalize_execution(value: Any) -> Any:
    if not _known(value):
        return UNKNOWN
    execution = _closed_mapping(value, field_name="execution", allowed=NESTED_FIELDS["execution"])
    normalized = {
        key: _bounded_string(execution.get(key), field_name=f"execution.{key}", max_length=256)
        for key in EXECUTION_FIELDS
    }
    return normalized if any(_known(value) for value in normalized.values()) else UNKNOWN


def _normalize_usage(value: Any) -> Any:
    if not _known(value):
        return UNKNOWN
    usage = _closed_mapping(value, field_name="usage", allowed=NESTED_FIELDS["usage"])
    normalized: dict[str, Any] = {}
    for key in USAGE_FIELDS:
        item = usage.get(key, UNKNOWN)
        normalized[key] = (
            _nonnegative_int(item, field_name=f"usage.{key}") if _known(item) else UNKNOWN
        )
    cost = usage.get("cost", UNKNOWN)
    if not _known(cost):
        normalized["cost"] = UNKNOWN
    else:
        cost_value = _closed_mapping(cost, field_name="usage.cost", allowed=NESTED_FIELDS["cost"])
        amount = cost_value.get("amount", UNKNOWN)
        if not _known(amount):
            normalized["cost"] = UNKNOWN
        else:
            normalized["cost"] = {
                "amount": format(_decimal(amount, field_name="usage.cost.amount"), "f"),
                "currency": _bounded_string(cost_value.get("currency"), field_name="usage.cost.currency", max_length=32, required=True),
                "unit": _bounded_string(cost_value.get("unit"), field_name="usage.cost.unit", max_length=64, required=True),
            }
    if all(not _known(normalized[key]) for key in USAGE_FIELDS) and normalized["cost"] == UNKNOWN:
        return UNKNOWN
    return normalized


def validate_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one closed, provider-neutral source event."""
    if not isinstance(raw, Mapping):
        raise TelemetryError("event must be an object")
    event = _closed_mapping(raw, field_name="event", allowed=TOP_LEVEL_FIELDS)
    if event.get("schema_version") != SCHEMA_VERSION:
        raise TelemetryError(f"schema_version must be {SCHEMA_VERSION}")
    event["source"] = _bounded_string(event.get("source"), field_name="source", max_length=128, required=True)
    event["source_event_id"] = _bounded_string(event.get("source_event_id"), field_name="source_event_id", required=True)
    event["observed_at"] = _bounded_string(event.get("observed_at"), field_name="observed_at", max_length=64, required=True)
    if event.get("series") not in SERIES:
        raise TelemetryError(f"series must be one of {sorted(SERIES)}")
    repository = _bounded_string(event.get("repository"), field_name="repository", max_length=256, required=True)
    if "/" not in repository:
        raise TelemetryError("repository must be owner/name")
    event["repository"] = repository
    for key in ("task_id", "pr_number", "lineage_id", "generation_id", "replaces_generation_id", "head_sha", "attempt_id"):
        event[key] = _identity(event.get(key))
    if _known(event.get("event_type")):
        event["event_type"] = _bounded_string(event["event_type"], field_name="event_type", max_length=256)
    else:
        event["event_type"] = UNKNOWN

    event["execution"] = _normalize_execution(event.get("execution", UNKNOWN))
    event["usage"] = _normalize_usage(event.get("usage", UNKNOWN))

    timing = event.get("timing")
    if timing is not None:
        timing = _closed_mapping(timing, field_name="timing", allowed=NESTED_FIELDS["timing"])
        if _known(timing.get("stage")):
            timing["stage"] = _bounded_string(timing["stage"], field_name="timing.stage", max_length=256)
        for key in ("duration_ms", "estimate_ms"):
            if _known(timing.get(key)):
                timing[key] = _nonnegative_int(timing[key], field_name=f"timing.{key}")
            elif key in timing:
                timing[key] = UNKNOWN
        event["timing"] = timing

    operator = event.get("operator")
    if operator is not None:
        operator = _closed_mapping(operator, field_name="operator", allowed=NESTED_FIELDS["operator"])
        required = operator.get("required")
        if required is not None and not isinstance(required, bool):
            raise TelemetryError("operator.required must be boolean when supplied")
        category = operator.get("category")
        if required is True and category not in OPERATOR_CATEGORIES:
            raise TelemetryError("operator.category must be exact for operator-required events")
        if _known(operator.get("duration_ms")):
            operator["duration_ms"] = _nonnegative_int(operator["duration_ms"], field_name="operator.duration_ms")
        elif "duration_ms" in operator:
            operator["duration_ms"] = UNKNOWN
        operator["action_id"] = _bounded_string(operator.get("action_id"), field_name="operator.action_id")
        operator["gate_id"] = _bounded_string(operator.get("gate_id"), field_name="operator.gate_id", max_length=256)
        if "override" in operator and not isinstance(operator["override"], bool):
            raise TelemetryError("operator.override must be boolean when supplied")
        event["operator"] = operator

    review = event.get("review")
    if review is not None:
        review = _closed_mapping(review, field_name="review", allowed=NESTED_FIELDS["review"])
        for key in ("round_id", "review_id", "exact_head_sha"):
            review[key] = _bounded_string(review.get(key), field_name=f"review.{key}")
        phase = review.get("phase")
        if phase is not None and phase not in REVIEW_PHASES:
            raise TelemetryError(f"review.phase must be one of {sorted(REVIEW_PHASES)}")
        event["review"] = review

    outcome = event.get("outcome")
    if outcome is not None:
        outcome = _closed_mapping(outcome, field_name="outcome", allowed=NESTED_FIELDS["outcome"])
        outcome["kind"] = _bounded_string(outcome.get("kind"), field_name="outcome.kind", max_length=128)
        outcome["evidence_id"] = _bounded_string(outcome.get("evidence_id"), field_name="outcome.evidence_id")
        for key in ("authoritative", "terminal"):
            if key in outcome and not isinstance(outcome[key], bool):
                raise TelemetryError(f"outcome.{key} must be boolean")
        event["outcome"] = outcome

    size = event.get("size")
    if size is not None:
        size = _closed_mapping(size, field_name="size", allowed=NESTED_FIELDS["size"])
        for key in NESTED_FIELDS["size"]:
            if _known(size.get(key)):
                size[key] = _nonnegative_int(size[key], field_name=f"size.{key}")
            elif key in size:
                size[key] = UNKNOWN
        event["size"] = size
    return event


def safe_append_event(path: str | os.PathLike[str], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Best-effort append that never raises into lifecycle code."""
    try:
        event = validate_event(raw)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        return {"telemetry_written": True, "degraded": False, "error": None}
    except Exception as exc:
        return {"telemetry_written": False, "degraded": True, "error": f"{type(exc).__name__}: {exc}"}


def safe_append_events(path: str | os.PathLike[str], events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    written = 0
    errors: list[str] = []
    for event in events:
        result = safe_append_event(path, event)
        if result["telemetry_written"]:
            written += 1
        else:
            errors.append(str(result["error"]))
    return {
        "telemetry_written": not errors,
        "written_event_count": written,
        "degraded": bool(errors),
        "errors": errors,
    }


def _execution_key(execution: Any) -> str:
    if execution == UNKNOWN:
        return UNKNOWN
    return json.dumps({key: execution.get(key, UNKNOWN) for key in EXECUTION_FIELDS}, sort_keys=True, separators=(",", ":"))


def _execution_identity(execution: Any) -> Any:
    if execution == UNKNOWN:
        return UNKNOWN
    return {key: execution.get(key, UNKNOWN) for key in EXECUTION_FIELDS}


def _scoped_local_id(event: Mapping[str, Any], value: Any) -> tuple[str, str, str]:
    """Namespace a source-local identifier by its owning source/execution identity."""
    return (str(event["source"]), _execution_key(event.get("execution", UNKNOWN)), str(value))


def _scoped_id_record(scoped: tuple[str, str, str], *, field_name: str) -> dict[str, Any]:
    source, execution_key, value = scoped
    identity = UNKNOWN if execution_key == UNKNOWN else json.loads(execution_key)
    return {"source": source, "execution": identity, field_name: value}


@dataclass
class ExecutionAccumulator:
    identity: Any
    event_count: int = 0
    attempts: set[tuple[str, str, str]] = field(default_factory=set)
    unknown_attempt_events: int = 0
    review_rounds: set[Any] = field(default_factory=set)
    usage_sums: Counter[str] = field(default_factory=Counter)
    usage_known_events: Counter[str] = field(default_factory=Counter)
    usage_unknown_events: Counter[str] = field(default_factory=Counter)
    cost_sums: dict[tuple[str, str], Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    cost_known_events: int = 0
    cost_unknown_events: int = 0
    operator_actions: set[tuple[str, str, str]] = field(default_factory=set)
    operator_unknown_action_events: int = 0
    operator_count: int = 0
    terminal_outcomes: set[str] = field(default_factory=set)

    def add(self, event: Mapping[str, Any], *, operator_counted: bool) -> None:
        self.event_count += 1
        attempt = event.get("attempt_id", UNKNOWN)
        if _known(attempt):
            self.attempts.add(_scoped_local_id(event, attempt))
        else:
            self.unknown_attempt_events += 1
        review = event.get("review") or {}
        if _known(review.get("round_id")):
            self.review_rounds.add(review["round_id"])
        usage = event.get("usage", UNKNOWN)
        if usage == UNKNOWN:
            for key in USAGE_FIELDS:
                self.usage_unknown_events[key] += 1
            self.cost_unknown_events += 1
        else:
            for key in USAGE_FIELDS:
                if _known(usage.get(key)):
                    self.usage_sums[key] += int(usage[key])
                    self.usage_known_events[key] += 1
                else:
                    self.usage_unknown_events[key] += 1
            cost = usage.get("cost", UNKNOWN)
            if cost != UNKNOWN:
                unit = (str(cost["currency"]), str(cost["unit"]))
                self.cost_sums[unit] += Decimal(str(cost["amount"]))
                self.cost_known_events += 1
            else:
                self.cost_unknown_events += 1
        operator = event.get("operator") or {}
        if operator_counted:
            self.operator_count += 1
            if _known(operator.get("action_id")):
                self.operator_actions.add(_scoped_local_id(event, operator["action_id"]))
            else:
                self.operator_unknown_action_events += 1
        outcome = event.get("outcome") or {}
        if outcome.get("authoritative") is True and outcome.get("terminal") is True and _known(outcome.get("kind")):
            self.terminal_outcomes.add(str(outcome["kind"]))

    def record(self) -> dict[str, Any]:
        usage: dict[str, Any] = {}
        for key in USAGE_FIELDS:
            known = self.usage_known_events.get(key, 0)
            usage[key] = {
                "attributed_value": self.usage_sums.get(key, 0) if known else UNKNOWN,
                "known_event_count": known,
                "unknown_event_count": self.usage_unknown_events.get(key, 0),
            }
        costs = [
            {"currency": currency, "unit": unit, "amount": format(amount, "f")}
            for (currency, unit), amount in sorted(self.cost_sums.items())
        ]
        usage["cost"] = {
            "exact_source_units": costs if self.cost_known_events else UNKNOWN,
            "known_event_count": self.cost_known_events,
            "unknown_event_count": self.cost_unknown_events,
        }
        return {
            "identity": self.identity,
            "event_count": self.event_count,
            "attempts": {
                "exact_ids": sorted((item[2] for item in self.attempts), key=str),
                "exact_scoped_ids": [
                    _scoped_id_record(item, field_name="attempt_id")
                    for item in sorted(self.attempts, key=str)
                ],
                "exact_count": len(self.attempts),
                "unknown_event_count": self.unknown_attempt_events,
            },
            "review_round_count": len(self.review_rounds),
            "operator_intervention_count": self.operator_count,
            "operator_action_ids": sorted((item[2] for item in self.operator_actions), key=str),
            "operator_action_scoped_ids": [
                _scoped_id_record(item, field_name="action_id")
                for item in sorted(self.operator_actions, key=str)
            ],
            "operator_unknown_action_events": self.operator_unknown_action_events,
            "usage": usage,
            "terminal_outcomes": sorted(self.terminal_outcomes) if self.terminal_outcomes else UNKNOWN,
        }


@dataclass
class GenerationAccumulator:
    repository: str
    lineage_id: Any
    generation_id: Any
    series: str
    task_ids: set[Any] = field(default_factory=set)
    pr_numbers: set[Any] = field(default_factory=set)
    heads: set[Any] = field(default_factory=set)
    replacement_ids: set[Any] = field(default_factory=set)
    event_count: int = 0
    source_events: list[dict[str, Any]] = field(default_factory=list)
    attempts: set[tuple[str, str, str]] = field(default_factory=set)
    unknown_attempt_events: int = 0
    review_rounds: set[Any] = field(default_factory=set)
    review_ids: set[Any] = field(default_factory=set)
    review_phase_counts: Counter[str] = field(default_factory=Counter)
    operator_counts: Counter[str] = field(default_factory=Counter)
    operator_duration_ms: Counter[str] = field(default_factory=Counter)
    operator_duration_unknown: Counter[str] = field(default_factory=Counter)
    operator_action_ids: set[tuple[str, str, str]] = field(default_factory=set)
    operator_unknown_action_events: int = 0
    override_gate_counts: Counter[str] = field(default_factory=Counter)
    unknown_override_gate_events: int = 0
    usage_sums: Counter[str] = field(default_factory=Counter)
    usage_known_events: Counter[str] = field(default_factory=Counter)
    usage_unknown_events: Counter[str] = field(default_factory=Counter)
    cost_sums: dict[tuple[str, str], Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    cost_known_events: int = 0
    cost_unknown_events: int = 0
    stage_durations: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    unknown_stage_duration_events: Counter[str] = field(default_factory=Counter)
    stage_estimates: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    stage_estimate_errors: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    unknown_stage_estimate_events: Counter[str] = field(default_factory=Counter)
    unknown_stage_estimate_error_events: Counter[str] = field(default_factory=Counter)
    terminal_outcomes: set[str] = field(default_factory=set)
    size_values: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    execution_buckets: dict[str, ExecutionAccumulator] = field(default_factory=dict)

    def add(self, event: Mapping[str, Any]) -> None:
        self.event_count += 1
        self.source_events.append({"source": event["source"], "source_event_id": event["source_event_id"]})
        for field_name, target in (
            ("task_id", self.task_ids), ("pr_number", self.pr_numbers),
            ("head_sha", self.heads), ("replaces_generation_id", self.replacement_ids),
        ):
            value = event.get(field_name, UNKNOWN)
            if _known(value):
                target.add(value)
        attempt = event.get("attempt_id", UNKNOWN)
        if _known(attempt):
            self.attempts.add(_scoped_local_id(event, attempt))
        else:
            self.unknown_attempt_events += 1
        review = event.get("review") or {}
        if _known(review.get("round_id")):
            self.review_rounds.add(review["round_id"])
        if _known(review.get("review_id")):
            self.review_ids.add(review["review_id"])
        if review.get("phase") in REVIEW_PHASES:
            self.review_phase_counts[review["phase"]] += 1

        operator = event.get("operator") or {}
        operator_counted = False
        if operator.get("required") is True:
            action_id = operator.get("action_id", UNKNOWN)
            if _known(action_id):
                scoped_action = _scoped_local_id(event, action_id)
                if scoped_action not in self.operator_action_ids:
                    self.operator_action_ids.add(scoped_action)
                    operator_counted = True
            else:
                self.operator_unknown_action_events += 1
                operator_counted = True
            if operator_counted:
                category = operator["category"]
                self.operator_counts[category] += 1
                if _known(operator.get("duration_ms")):
                    self.operator_duration_ms[category] += int(operator["duration_ms"])
                else:
                    self.operator_duration_unknown[category] += 1
                if operator.get("override") is True:
                    gate_id = operator.get("gate_id", UNKNOWN)
                    if _known(gate_id):
                        self.override_gate_counts[str(gate_id)] += 1
                    else:
                        self.unknown_override_gate_events += 1

        usage = event.get("usage", UNKNOWN)
        if usage == UNKNOWN:
            for key in USAGE_FIELDS:
                self.usage_unknown_events[key] += 1
            self.cost_unknown_events += 1
        else:
            for key in USAGE_FIELDS:
                if _known(usage.get(key)):
                    self.usage_sums[key] += int(usage[key])
                    self.usage_known_events[key] += 1
                else:
                    self.usage_unknown_events[key] += 1
            cost = usage.get("cost", UNKNOWN)
            if cost != UNKNOWN:
                unit = (str(cost["currency"]), str(cost["unit"]))
                self.cost_sums[unit] += Decimal(str(cost["amount"]))
                self.cost_known_events += 1
            else:
                self.cost_unknown_events += 1

        timing = event.get("timing") or {}
        stage = str(timing.get("stage") or event.get("event_type") or UNKNOWN)
        duration_known = _known(timing.get("duration_ms"))
        estimate_known = _known(timing.get("estimate_ms"))
        if duration_known:
            duration_ms = int(timing["duration_ms"])
            self.stage_durations[stage].append(duration_ms)
        elif timing:
            self.unknown_stage_duration_events[stage] += 1
        if estimate_known:
            estimate_ms = int(timing["estimate_ms"])
            self.stage_estimates[stage].append(estimate_ms)
        elif timing:
            self.unknown_stage_estimate_events[stage] += 1
        if timing and duration_known and estimate_known:
            self.stage_estimate_errors[stage].append(duration_ms - estimate_ms)
        elif timing:
            self.unknown_stage_estimate_error_events[stage] += 1
        outcome = event.get("outcome") or {}
        if outcome.get("authoritative") is True and outcome.get("terminal") is True and _known(outcome.get("kind")):
            self.terminal_outcomes.add(str(outcome["kind"]))
        size = event.get("size") or {}
        for key in NESTED_FIELDS["size"]:
            if _known(size.get(key)):
                self.size_values[key].add(int(size[key]))

        execution = event.get("execution", UNKNOWN)
        bucket_key = _execution_key(execution)
        if bucket_key not in self.execution_buckets:
            self.execution_buckets[bucket_key] = ExecutionAccumulator(_execution_identity(execution))
        self.execution_buckets[bucket_key].add(event, operator_counted=operator_counted)

    def record(self) -> dict[str, Any]:
        terminal = next(iter(self.terminal_outcomes)) if len(self.terminal_outcomes) == 1 else UNKNOWN
        repeated_gates = {gate: count for gate, count in sorted(self.override_gate_counts.items()) if count > 1}
        stage_names = (
            set(self.stage_durations) | set(self.unknown_stage_duration_events) | set(self.stage_estimates)
            | set(self.stage_estimate_errors) | set(self.unknown_stage_estimate_events)
            | set(self.unknown_stage_estimate_error_events)
        )
        durations = {
            stage: {
                "known_ms": list(self.stage_durations.get(stage, [])),
                "unknown_event_count": self.unknown_stage_duration_events.get(stage, 0),
                "estimate_known_ms": list(self.stage_estimates.get(stage, [])),
                "estimate_unknown_event_count": self.unknown_stage_estimate_events.get(stage, 0),
                "estimate_error_known_ms": list(self.stage_estimate_errors.get(stage, [])),
                "estimate_error_unknown_event_count": self.unknown_stage_estimate_error_events.get(stage, 0),
            }
            for stage in sorted(stage_names)
        }
        usage: dict[str, Any] = {}
        for key in USAGE_FIELDS:
            known = self.usage_known_events.get(key, 0)
            usage[key] = {
                "attributed_value": self.usage_sums.get(key, 0) if known else UNKNOWN,
                "known_event_count": known,
                "unknown_event_count": self.usage_unknown_events.get(key, 0),
            }
        costs = [
            {"currency": currency, "unit": unit, "amount": format(amount, "f")}
            for (currency, unit), amount in sorted(self.cost_sums.items())
        ]
        usage["cost"] = {
            "exact_source_units": costs if self.cost_known_events else UNKNOWN,
            "known_event_count": self.cost_known_events,
            "unknown_event_count": self.cost_unknown_events,
        }
        known_execution = [bucket.record()["identity"] for key, bucket in sorted(self.execution_buckets.items()) if key != UNKNOWN]
        return {
            "schema_version": "lifecycle-economics-generation-record/v1",
            "authority": "observational_only",
            "repository": self.repository,
            "series": self.series,
            "lineage_id": self.lineage_id,
            "generation_id": self.generation_id,
            "replaces_generation_ids": sorted(self.replacement_ids, key=str),
            "task_ids": sorted(self.task_ids, key=str),
            "pr_numbers": sorted(self.pr_numbers, key=str),
            "head_shas": sorted(self.heads, key=str),
            "event_count": self.event_count,
            "source_events": self.source_events,
            "attempts": {
                "exact_ids": sorted((item[2] for item in self.attempts), key=str),
                "exact_scoped_ids": [
                    _scoped_id_record(item, field_name="attempt_id")
                    for item in sorted(self.attempts, key=str)
                ],
                "exact_count": len(self.attempts),
                "unknown_event_count": self.unknown_attempt_events,
            },
            "review": {"round_ids": sorted(self.review_rounds, key=str), "round_count": len(self.review_rounds), "review_ids": sorted(self.review_ids, key=str), "phase_counts": dict(sorted(self.review_phase_counts.items()))},
            "operator": {
                "intervention_count": sum(self.operator_counts.values()),
                "category_counts": dict(sorted(self.operator_counts.items())),
                "category_duration_ms": dict(sorted(self.operator_duration_ms.items())),
                "category_duration_unknown_events": dict(sorted(self.operator_duration_unknown.items())),
                "action_ids": sorted((item[2] for item in self.operator_action_ids), key=str),
                "action_scoped_ids": [
                    _scoped_id_record(item, field_name="action_id")
                    for item in sorted(self.operator_action_ids, key=str)
                ],
                "unknown_action_event_count": self.operator_unknown_action_events,
                "override_count": sum(self.override_gate_counts.values()) + self.unknown_override_gate_events,
                "override_gate_counts": dict(sorted(self.override_gate_counts.items())),
                "repeated_same_gate": repeated_gates,
                "unknown_override_gate_events": self.unknown_override_gate_events,
            },
            "usage": usage,
            "timing": {"stages": durations},
            "execution": known_execution if known_execution else UNKNOWN,
            "execution_economics": [bucket.record() for _, bucket in sorted(self.execution_buckets.items())],
            "size": {key: sorted(values) for key, values in sorted(self.size_values.items())},
            "terminal_outcome": terminal,
            "terminal_outcome_conflict": sorted(self.terminal_outcomes) if len(self.terminal_outcomes) > 1 else [],
        }


def collect(events: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    groups: dict[tuple[Any, ...], GenerationAccumulator] = {}
    repository_health_events = 0
    duplicate_events = 0
    for raw in events:
        event = validate_event(raw)
        event_key = (event["source"], event["source_event_id"])
        if event_key in seen:
            duplicate_events += 1
            continue
        seen.add(event_key)
        if event["series"] == REPOSITORY_HEALTH:
            repository_health_events += 1
        lineage, generation = event["lineage_id"], event["generation_id"]
        if _known(lineage) and _known(generation):
            group_key = (event["repository"], event["series"], lineage, generation)
        else:
            group_key = (event["repository"], event["series"], UNKNOWN, event["source"], event["source_event_id"])
        groups.setdefault(group_key, GenerationAccumulator(
            repository=event["repository"], series=event["series"],
            lineage_id=lineage if _known(lineage) else UNKNOWN,
            generation_id=generation if _known(generation) else UNKNOWN,
        )).add(event)
    records = [group.record() for _, group in sorted(groups.items(), key=lambda item: tuple(map(str, item[0])))]
    return records, build_report(records, duplicate_events=duplicate_events, repository_health_events=repository_health_events)


def _percentile(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(percentile * len(ordered))) - 1]


def _distribution(values: Sequence[int], *, unknown_count: int, low_sample_threshold: int) -> dict[str, Any]:
    return {
        "count": len(values), "unknown_count": unknown_count,
        "p50": _percentile(values, 0.50), "p90": _percentile(values, 0.90),
        "low_sample": len(values) < low_sample_threshold,
    }


def _decimal_distribution(values: Sequence[Decimal], *, unknown_count: int, low_sample_threshold: int) -> dict[str, Any]:
    if values:
        ordered = sorted(values)
        p50 = ordered[max(1, math.ceil(0.50 * len(ordered))) - 1]
        p90 = ordered[max(1, math.ceil(0.90 * len(ordered))) - 1]
        p50_value: Any = format(p50, "f")
        p90_value: Any = format(p90, "f")
    else:
        p50_value = p90_value = None
    return {
        "count": len(values), "unknown_count": unknown_count,
        "p50": p50_value, "p90": p90_value,
        "low_sample": len(values) < low_sample_threshold,
    }


def build_report(records: Sequence[Mapping[str, Any]], *, duplicate_events: int = 0, repository_health_events: int = 0, low_sample_threshold: int = 5) -> dict[str, Any]:
    """Build diagnostic-only timing/economics/outcome summaries."""
    pr_flow = [record for record in records if record.get("series") == PR_FLOW]
    stages: dict[str, list[int]] = defaultdict(list)
    stage_unknowns: Counter[str] = Counter()
    stage_estimates: dict[str, list[int]] = defaultdict(list)
    stage_estimate_unknowns: Counter[str] = Counter()
    stage_estimate_errors: dict[str, list[int]] = defaultdict(list)
    stage_estimate_error_unknowns: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    attempts: list[int] = []
    attempt_unknown_generations = 0
    attempt_unknown_events = 0
    review_rounds: list[int] = []
    operator_interventions: list[int] = []
    operator_category_counts: Counter[str] = Counter()
    operator_category_durations: dict[str, list[int]] = defaultdict(list)
    operator_duration_unknowns: Counter[str] = Counter()
    usage_values: dict[str, list[int]] = defaultdict(list)
    usage_unknown_generations: Counter[str] = Counter()
    usage_unknown_events: Counter[str] = Counter()
    cost_values: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    cost_unknown_generations = 0
    cost_unknown_events = 0
    by_execution: dict[str, dict[str, Any]] = {}

    for record in pr_flow:
        for stage, payload in (record.get("timing", {}).get("stages", {}) or {}).items():
            stages[stage].extend(payload.get("known_ms", []))
            stage_unknowns[stage] += int(payload.get("unknown_event_count", 0))
            stage_estimates[stage].extend(payload.get("estimate_known_ms", []))
            stage_estimate_unknowns[stage] += int(payload.get("estimate_unknown_event_count", 0))
            stage_estimate_errors[stage].extend(payload.get("estimate_error_known_ms", []))
            stage_estimate_error_unknowns[stage] += int(payload.get("estimate_error_unknown_event_count", 0))
        outcomes[str(record.get("terminal_outcome", UNKNOWN))] += 1
        attempt_payload = record.get("attempts", {})
        unknown_attempts = int(attempt_payload.get("unknown_event_count", 0))
        attempt_unknown_events += unknown_attempts
        if unknown_attempts:
            attempt_unknown_generations += 1
        else:
            attempts.append(int(attempt_payload.get("exact_count", 0)))
        review_rounds.append(int(record.get("review", {}).get("round_count", 0)))
        operator_interventions.append(int(record.get("operator", {}).get("intervention_count", 0)))
        for category, count in (record.get("operator", {}).get("category_counts", {}) or {}).items():
            operator_category_counts[category] += int(count)
        for category, duration in (record.get("operator", {}).get("category_duration_ms", {}) or {}).items():
            operator_category_durations[category].append(int(duration))
        for category, count in (record.get("operator", {}).get("category_duration_unknown_events", {}) or {}).items():
            operator_duration_unknowns[category] += int(count)
        for key in USAGE_FIELDS:
            payload = record.get("usage", {}).get(key, {})
            if _known(payload.get("attributed_value")):
                usage_values[key].append(int(payload["attributed_value"]))
            else:
                usage_unknown_generations[key] += 1
            usage_unknown_events[key] += int(payload.get("unknown_event_count", 0))
        cost_payload = record.get("usage", {}).get("cost", {})
        exact_units = cost_payload.get("exact_source_units", UNKNOWN)
        cost_unknown_events += int(cost_payload.get("unknown_event_count", 0))
        if exact_units == UNKNOWN:
            cost_unknown_generations += 1
        else:
            for item in exact_units:
                cost_values[(item["currency"], item["unit"])].append(Decimal(item["amount"]))

        for bucket in record.get("execution_economics", []):
            identity = bucket.get("identity", UNKNOWN)
            key = _execution_key(identity)
            entry = by_execution.setdefault(key, {
                "identity": identity, "generation_count": 0, "outcomes": Counter(),
                "attempts": [], "attempt_unknown_generations": 0, "attempt_unknown_events": 0, "review_rounds": [], "operator": [],
                "usage": defaultdict(list), "usage_unknown": Counter(),
                "cost": defaultdict(list), "cost_unknown": 0, "cost_unknown_events": 0,
                "usage_unknown_events": Counter(), "source_terminal_outcomes": Counter(),
            })
            entry["generation_count"] += 1
            entry["outcomes"][str(record.get("terminal_outcome", UNKNOWN))] += 1
            bucket_attempts = bucket.get("attempts", {})
            bucket_unknown_attempts = int(bucket_attempts.get("unknown_event_count", 0))
            entry["attempt_unknown_events"] += bucket_unknown_attempts
            if bucket_unknown_attempts:
                entry["attempt_unknown_generations"] += 1
            else:
                entry["attempts"].append(int(bucket_attempts.get("exact_count", 0)))
            entry["review_rounds"].append(int(bucket.get("review_round_count", 0)))
            entry["operator"].append(int(bucket.get("operator_intervention_count", 0)))
            for metric in USAGE_FIELDS:
                metric_payload = bucket.get("usage", {}).get(metric, {})
                if _known(metric_payload.get("attributed_value")):
                    entry["usage"][metric].append(int(metric_payload["attributed_value"]))
                else:
                    entry["usage_unknown"][metric] += 1
                entry["usage_unknown_events"][metric] += int(metric_payload.get("unknown_event_count", 0))
            bucket_cost = bucket.get("usage", {}).get("cost", {})
            exact = bucket_cost.get("exact_source_units", UNKNOWN)
            entry["cost_unknown_events"] += int(bucket_cost.get("unknown_event_count", 0))
            source_terminal = bucket.get("terminal_outcomes", UNKNOWN)
            if source_terminal != UNKNOWN:
                for outcome in source_terminal:
                    entry["source_terminal_outcomes"][str(outcome)] += 1
            if exact == UNKNOWN:
                entry["cost_unknown"] += 1
            else:
                for item in exact:
                    entry["cost"][(item["currency"], item["unit"])].append(Decimal(item["amount"]))

    timing = {
        stage: {
            "count": len(stages.get(stage, [])),
            "unknown_event_count": stage_unknowns.get(stage, 0),
            "p50_ms": _percentile(stages.get(stage, []), 0.50),
            "p90_ms": _percentile(stages.get(stage, []), 0.90),
            "low_sample": len(stages.get(stage, [])) < low_sample_threshold,
            "estimate_ms": {
                "count": len(stage_estimates.get(stage, [])),
                "unknown_event_count": stage_estimate_unknowns.get(stage, 0),
                "p50_ms": _percentile(stage_estimates.get(stage, []), 0.50),
                "p90_ms": _percentile(stage_estimates.get(stage, []), 0.90),
                "low_sample": len(stage_estimates.get(stage, [])) < low_sample_threshold,
            },
            "estimate_error_ms": {
                "count": len(stage_estimate_errors.get(stage, [])),
                "unknown_event_count": stage_estimate_error_unknowns.get(stage, 0),
                "p50_ms": _percentile(stage_estimate_errors.get(stage, []), 0.50),
                "p90_ms": _percentile(stage_estimate_errors.get(stage, []), 0.90),
                "low_sample": len(stage_estimate_errors.get(stage, [])) < low_sample_threshold,
            },
        }
        for stage in sorted(
            set(stages) | set(stage_unknowns) | set(stage_estimates) | set(stage_estimate_unknowns)
            | set(stage_estimate_errors) | set(stage_estimate_error_unknowns)
        )
    }
    operator_report = {}
    for category in sorted(set(operator_category_counts) | set(operator_category_durations) | set(operator_duration_unknowns)):
        durations = operator_category_durations.get(category, [])
        operator_report[category] = {
            "intervention_count": operator_category_counts.get(category, 0),
            "duration_ms": _distribution(durations, unknown_count=operator_duration_unknowns.get(category, 0), low_sample_threshold=low_sample_threshold),
        }
    economics_usage = {
        metric: _distribution(usage_values.get(metric, []), unknown_count=usage_unknown_generations.get(metric, 0), low_sample_threshold=low_sample_threshold)
        | {"unknown_event_count": usage_unknown_events.get(metric, 0)}
        for metric in USAGE_FIELDS
    }
    economics_cost = [
        {
            "currency": currency, "unit": unit,
            "amount": _decimal_distribution(values, unknown_count=0, low_sample_threshold=low_sample_threshold),
        }
        for (currency, unit), values in sorted(cost_values.items())
    ]
    execution_report = []
    for key in sorted(by_execution):
        entry = by_execution[key]
        execution_report.append({
            "identity": entry["identity"],
            "generation_count": entry["generation_count"],
            "low_sample": entry["generation_count"] < low_sample_threshold,
            "generation_outcomes": dict(sorted(entry["outcomes"].items())),
            "source_terminal_outcomes": dict(sorted(entry["source_terminal_outcomes"].items())) if entry["source_terminal_outcomes"] else UNKNOWN,
            "attempts": _distribution(entry["attempts"], unknown_count=entry["attempt_unknown_generations"], low_sample_threshold=low_sample_threshold)
            | {"unknown_event_count": entry["attempt_unknown_events"]},
            "review_rounds": _distribution(entry["review_rounds"], unknown_count=0, low_sample_threshold=low_sample_threshold),
            "operator_interventions": _distribution(entry["operator"], unknown_count=0, low_sample_threshold=low_sample_threshold),
            "usage": {
                metric: _distribution(entry["usage"].get(metric, []), unknown_count=entry["usage_unknown"].get(metric, 0), low_sample_threshold=low_sample_threshold)
                | {"unknown_event_count": entry["usage_unknown_events"].get(metric, 0)}
                for metric in USAGE_FIELDS
            },
            "cost": [
                {"currency": currency, "unit": unit, "amount": _decimal_distribution(values, unknown_count=0, low_sample_threshold=low_sample_threshold)}
                for (currency, unit), values in sorted(entry["cost"].items())
            ],
            "cost_unknown_generation_count": entry["cost_unknown"],
            "cost_unknown_event_count": entry["cost_unknown_events"],
        })

    return {
        "schema_version": "lifecycle-economics-diagnostic-report/v1",
        "authority": "diagnostic_only",
        "generation_count": len(records),
        "pr_flow_generation_count": len(pr_flow),
        "repository_health_generation_count": len(records) - len(pr_flow),
        "repository_health_event_count": repository_health_events,
        "deduplicated_source_event_count": duplicate_events,
        "timing": timing,
        "outcomes": {"counts": dict(sorted(outcomes.items())), "low_sample": len(pr_flow) < low_sample_threshold},
        "flow_economics": {
            "attempts": _distribution(attempts, unknown_count=attempt_unknown_generations, low_sample_threshold=low_sample_threshold)
            | {"unknown_event_count": attempt_unknown_events},
            "review_rounds": _distribution(review_rounds, unknown_count=0, low_sample_threshold=low_sample_threshold),
            "operator_interventions": _distribution(operator_interventions, unknown_count=0, low_sample_threshold=low_sample_threshold),
            "usage": economics_usage,
            "cost": economics_cost,
            "cost_unknown_generation_count": cost_unknown_generations,
            "cost_unknown_event_count": cost_unknown_events,
        },
        "operator": operator_report,
        "by_execution": execution_report,
        "low_sample_threshold": low_sample_threshold,
        "eligibility": UNKNOWN,
        "routing_recommendation": UNKNOWN,
        "productivity_score": UNKNOWN,
    }


def _single_task_id(lifecycle: Mapping[str, Any]) -> Any:
    task_ids = lifecycle.get("task_ids") or []
    return task_ids[0] if isinstance(task_ids, list) and len(task_ids) == 1 else UNKNOWN


def _lifecycle_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    json_method = getattr(value, "json", None)
    if callable(json_method):
        result = json_method()
        if isinstance(result, Mapping):
            return dict(result)
    result = {}
    for key in ("number", "head", "state", "task_ids"):
        if hasattr(value, key):
            raw = getattr(value, key)
            result[key] = getattr(raw, "value", raw)
    return result


def _formal_review_verdict(review: Mapping[str, Any]) -> Any:
    if review.get("verdict") in {"BLOCK", "MERGE"}:
        return review["verdict"]
    body = review.get("body")
    if not isinstance(body, str):
        return UNKNOWN
    match = re.search(r"(?im)^\s*VERDICT:\s*(BLOCK|MERGE)\s*$", body)
    return match.group(1).upper() if match else UNKNOWN


def events_from_authoritative_pr_sources(*, repository: str, lifecycle: Any, raw_pr: Mapping[str, Any], reviews: Iterable[Mapping[str, Any]], comments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Adapt existing authoritative lifecycle/GitHub facts without copying source policy.

    Missing lineage/generation/execution economics remain UNKNOWN. The adapter carries only
    identifiers, timestamps, source-owned states/markers, sizes, and outcomes; bodies are parsed
    for formal verdict/override markers and then discarded.
    """
    life = _lifecycle_mapping(lifecycle)
    task_id = _single_task_id(life)
    pr_number = raw_pr.get("number", life.get("number", UNKNOWN))
    head_obj = raw_pr.get("head") if isinstance(raw_pr.get("head"), Mapping) else {}
    head = _identity(head_obj.get("sha") or raw_pr.get("head_sha") or life.get("head"))
    updated_at = raw_pr.get("updated_at") or raw_pr.get("merged_at") or raw_pr.get("closed_at")
    events: list[dict[str, Any]] = []
    if _known(updated_at):
        state = getattr(life.get("state"), "value", life.get("state", UNKNOWN))
        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source": "dish-pr-lifecycle",
            "source_event_id": f"github-pr:{raw_pr.get('id', pr_number)}:{head}:{state}",
            "observed_at": str(updated_at),
            "series": PR_FLOW,
            "repository": repository,
            "task_id": task_id,
            "pr_number": pr_number,
            "lineage_id": UNKNOWN,
            "generation_id": UNKNOWN,
            "head_sha": head,
            "event_type": str(state),
            "execution": UNKNOWN,
            "usage": UNKNOWN,
        }
        size = {
            key: raw_pr.get(source_key)
            for key, source_key in (("files_changed", "changed_files"), ("additions", "additions"), ("deletions", "deletions"))
            if _known(raw_pr.get(source_key))
        }
        if size:
            event["size"] = size
        merged_at = raw_pr.get("merged_at")
        closed_at = raw_pr.get("closed_at")
        if _known(merged_at):
            event["observed_at"] = str(merged_at)
            event["outcome"] = {"kind": "merged", "authoritative": True, "terminal": True, "evidence_id": f"github-pr:{raw_pr.get('id', pr_number)}:merged"}
        elif str(raw_pr.get("state", "")).lower() == "closed" and _known(closed_at):
            event["observed_at"] = str(closed_at)
            event["outcome"] = {"kind": "closed", "authoritative": True, "terminal": True, "evidence_id": f"github-pr:{raw_pr.get('id', pr_number)}:closed"}
        events.append(validate_event(event))

    for review in reviews:
        review_id = review.get("id") or review.get("node_id")
        submitted_at = review.get("submitted_at")
        commit_id = review.get("commit_id") or review.get("head_sha")
        verdict = _formal_review_verdict(review)
        if not (_known(review_id) and _known(submitted_at) and _known(commit_id) and verdict in {"BLOCK", "MERGE"}):
            continue
        review_event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source": "github-formal-review",
            "source_event_id": f"github-review:{review_id}",
            "observed_at": str(submitted_at),
            "series": PR_FLOW,
            "repository": repository,
            "task_id": task_id,
            "pr_number": pr_number,
            "lineage_id": UNKNOWN,
            "generation_id": UNKNOWN,
            "head_sha": commit_id,
            "event_type": "formal_review",
            "execution": UNKNOWN,
            "usage": UNKNOWN,
            "review": {
                "round_id": str(review_id), "review_id": str(review_id),
                "exact_head_sha": str(commit_id), "phase": "blocked" if verdict == "BLOCK" else "passed",
            },
        }
        body = review.get("body")
        if isinstance(body, str):
            override_match = OVERRIDE_RE.search(body)
            if override_match:
                canonical_gate = CANONICAL_OVERRIDE_GATE_RE.search(body)
                review_event["operator"] = {
                    "required": True,
                    "category": "override_waiver_permission_prompt",
                    "action_id": f"github-review:{review_id}:override",
                    "gate_id": canonical_gate.group("gate") if canonical_gate else UNKNOWN,
                    "override": True,
                    "duration_ms": UNKNOWN,
                }
        events.append(validate_event(review_event))

    for comment in comments:
        body = comment.get("body")
        comment_id = comment.get("id")
        created_at = comment.get("created_at")
        if not (isinstance(body, str) and _known(comment_id) and _known(created_at)):
            continue
        for marker in HUMAN_NOTICE_RE.finditer(body):
            category = NOTICE_CATEGORY.get(marker.group("kind"))
            if category is None:
                continue
            events.append(validate_event({
                "schema_version": SCHEMA_VERSION,
                "source": "github-human-notice",
                "source_event_id": f"github-comment:{comment_id}:human-notice:{marker.group('key')}",
                "observed_at": str(created_at),
                "series": PR_FLOW,
                "repository": repository,
                "task_id": task_id,
                "pr_number": pr_number,
                "lineage_id": UNKNOWN,
                "generation_id": UNKNOWN,
                "head_sha": marker.group("head").lower(),
                "event_type": f"human_notice:{marker.group('kind')}",
                "execution": UNKNOWN,
                "usage": UNKNOWN,
                "operator": {
                    "required": True, "category": category,
                    "action_id": marker.group("key"), "duration_ms": UNKNOWN,
                    "gate_id": UNKNOWN, "override": False,
                },
            }))
    return events


def safe_capture_pr(path: str | os.PathLike[str], *, github: Any, repository: str, pr_number: int, lifecycle: Any) -> dict[str, Any]:
    """Read current authoritative backend facts and append source events, fail-open."""
    try:
        raw_pr = github.get_pr(pr_number)
        reviews = github.get_reviews(pr_number)
        comments = github.get_comments(pr_number)
        events = events_from_authoritative_pr_sources(
            repository=repository, lifecycle=lifecycle, raw_pr=raw_pr, reviews=reviews, comments=comments,
        )
        result = safe_append_events(path, events)
        result["adapted_event_count"] = len(events)
        return result
    except Exception as exc:
        return {"telemetry_written": False, "written_event_count": 0, "adapted_event_count": 0, "degraded": True, "errors": [f"{type(exc).__name__}: {exc}"]}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TelemetryError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise TelemetryError(f"{path}:{line_number}: each line must be an object")
            events.append(value)
    return events


def _atomic_write_json(path: Path, value: Any, *, jsonl: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp = Path(handle.name)
        if jsonl:
            for item in value:
                handle.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
        else:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
    os.replace(tmp, path)


def _cmd_collect(args: argparse.Namespace) -> int:
    records, report = collect(_read_jsonl(Path(args.input)))
    _atomic_write_json(Path(args.records), records, jsonl=True)
    _atomic_write_json(Path(args.report), report)
    return 0


def _cmd_emit(args: argparse.Namespace) -> int:
    try:
        with Path(args.event_json).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        result = safe_append_event(args.output, raw) if isinstance(raw, dict) else {"telemetry_written": False, "degraded": True, "error": "TelemetryError: event JSON must be an object"}
    except Exception as exc:
        result = {"telemetry_written": False, "degraded": True, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_capture_pr(args: argparse.Namespace) -> int:
    try:
        from pr_lifecycle_support import AsanaREST, GitHubREST, JSONHTTPClient
        from pr_lifecycle import LifecycleEngine
        token = args.github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        if not token:
            raise TelemetryError("GitHub token is required for authoritative capture")
        http = JSONHTTPClient(timeout=args.http_timeout)
        github = GitHubREST(args.repo, token, api_root=args.github_api_root, http=http)
        asana_token = args.asana_token or os.getenv("ASANA_ACCESS_TOKEN")
        asana = AsanaREST(asana_token, http=http) if asana_token else None
        raw_pr = github.get_pr(args.pr_number)
        engine = LifecycleEngine(github, asana=asana, integration_authority=False, integration_capable=False)
        lifecycle = engine.inspect(raw_pr)
        result = safe_capture_pr(args.output, github=github, repository=args.repo, pr_number=args.pr_number, lifecycle=lifecycle)
    except Exception as exc:
        result = {"telemetry_written": False, "written_event_count": 0, "adapted_event_count": 0, "degraded": True, "errors": [f"{type(exc).__name__}: {exc}"]}
    print(json.dumps(result, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="derive observational records and diagnostics from JSONL events")
    collect_parser.add_argument("--input", required=True)
    collect_parser.add_argument("--records", required=True)
    collect_parser.add_argument("--report", required=True)
    collect_parser.set_defaults(func=_cmd_collect)
    emit_parser = subparsers.add_parser("emit", help="best-effort append one closed telemetry event")
    emit_parser.add_argument("--event-json", required=True)
    emit_parser.add_argument("--output", required=True)
    emit_parser.set_defaults(func=_cmd_emit)
    capture = subparsers.add_parser("capture-pr", help="one-shot authoritative GitHub/lifecycle source capture; no watcher or scheduler")
    capture.add_argument("--pr-number", required=True, type=int)
    capture.add_argument("--output", required=True)
    capture.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "marcogallotta/ai-tools"))
    capture.add_argument("--github-token", help=argparse.SUPPRESS)
    capture.add_argument("--asana-token", help=argparse.SUPPRESS)
    capture.add_argument("--github-api-root", default="https://api.github.com")
    capture.add_argument("--http-timeout", type=float, default=10.0)
    capture.set_defaults(func=_cmd_capture_pr)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
