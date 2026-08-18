#!/usr/bin/env python3
"""Observational lifecycle/operator economics telemetry.

This module is intentionally outside lifecycle authority. It accepts provider-neutral
source events, preserves exact/unknown attribution, and derives diagnostic records.
Telemetry writes are best-effort and never raise through ``safe_append_event``.
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
import tempfile
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "lifecycle-economics-source-event/v1"
UNKNOWN = "UNKNOWN"
PR_FLOW = "pr_flow"
REPOSITORY_HEALTH = "repository_health"
SERIES = {PR_FLOW, REPOSITORY_HEALTH}
OPERATOR_CATEGORIES = {
    "design_risk_product_decision",
    "manual_relay_or_queue_routing",
    "status_reconciliation_or_repair",
    "override_waiver_permission_prompt",
    "integration_merge_controller_babysitting",
    "workflow_incident_firefighting",
}
REVIEW_PHASES = {
    "dispatched",
    "blocked",
    "fix_started",
    "rereview_requested",
    "passed",
}
FORBIDDEN_PAYLOAD_KEYS = {
    "body",
    "chat",
    "content",
    "message",
    "prompt",
    "source_code",
    "transcript",
}


class TelemetryError(ValueError):
    """A telemetry input is malformed or non-attributable."""


def _known(value: Any) -> bool:
    return value is not None and value != "" and value != UNKNOWN


def _identity(value: Any) -> Any:
    return value if _known(value) else UNKNOWN


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


def _reject_payload_text(value: Any, *, path: str = "event") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key)
            if key_name.lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise TelemetryError(f"{path}.{key_name} is payload text and is not allowed in telemetry")
            _reject_payload_text(item, path=f"{path}.{key_name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_payload_text(item, path=f"{path}[{index}]")


def validate_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one provider-neutral source event without inventing missing identity."""
    event = dict(raw)
    _reject_payload_text(event)
    if event.get("schema_version") != SCHEMA_VERSION:
        raise TelemetryError(f"schema_version must be {SCHEMA_VERSION}")
    source = event.get("source")
    if not isinstance(source, str) or not source.strip():
        raise TelemetryError("source must be a non-empty provider-neutral source name")
    source_event_id = event.get("source_event_id")
    if not isinstance(source_event_id, str) or not source_event_id.strip():
        raise TelemetryError("source_event_id must be a non-empty source-owned identifier")
    series = event.get("series")
    if series not in SERIES:
        raise TelemetryError(f"series must be one of {sorted(SERIES)}")
    repository = event.get("repository")
    if not isinstance(repository, str) or "/" not in repository:
        raise TelemetryError("repository must be owner/name")
    observed_at = event.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at.strip():
        raise TelemetryError("observed_at must be an authoritative source timestamp")

    # Identity fields are explicitly UNKNOWN when absent; they are never synthesized.
    for key in ("task_id", "pr_number", "lineage_id", "generation_id", "replaces_generation_id", "head_sha", "attempt_id"):
        event[key] = _identity(event.get(key))

    timing = event.get("timing")
    if timing is not None:
        if not isinstance(timing, Mapping):
            raise TelemetryError("timing must be an object")
        timing = dict(timing)
        if "duration_ms" in timing and _known(timing.get("duration_ms")):
            timing["duration_ms"] = _nonnegative_int(timing["duration_ms"], field_name="timing.duration_ms")
        event["timing"] = timing

    operator = event.get("operator")
    if operator is not None:
        if not isinstance(operator, Mapping):
            raise TelemetryError("operator must be an object")
        operator = dict(operator)
        required = operator.get("required")
        if required is not None and not isinstance(required, bool):
            raise TelemetryError("operator.required must be boolean when supplied")
        category = operator.get("category")
        if required is True and category not in OPERATOR_CATEGORIES:
            raise TelemetryError("operator.category must be exact for operator-required events")
        if _known(operator.get("duration_ms")):
            operator["duration_ms"] = _nonnegative_int(operator["duration_ms"], field_name="operator.duration_ms")
        operator["action_id"] = _identity(operator.get("action_id"))
        operator["gate_id"] = _identity(operator.get("gate_id"))
        if "override" in operator and not isinstance(operator["override"], bool):
            raise TelemetryError("operator.override must be boolean when supplied")
        event["operator"] = operator

    review = event.get("review")
    if review is not None:
        if not isinstance(review, Mapping):
            raise TelemetryError("review must be an object")
        review = dict(review)
        review["round_id"] = _identity(review.get("round_id"))
        review["review_id"] = _identity(review.get("review_id"))
        review["exact_head_sha"] = _identity(review.get("exact_head_sha"))
        phase = review.get("phase")
        if phase is not None and phase not in REVIEW_PHASES:
            raise TelemetryError(f"review.phase must be one of {sorted(REVIEW_PHASES)}")
        event["review"] = review

    usage = event.get("usage")
    if usage is not None:
        if not isinstance(usage, Mapping):
            raise TelemetryError("usage must be an object")
        usage = dict(usage)
        for key in ("tool_calls", "input_tokens", "output_tokens", "total_tokens"):
            if _known(usage.get(key)):
                usage[key] = _nonnegative_int(usage[key], field_name=f"usage.{key}")
        cost = usage.get("cost")
        if cost is not None:
            if not isinstance(cost, Mapping):
                raise TelemetryError("usage.cost must be an object")
            cost = dict(cost)
            if _known(cost.get("amount")):
                cost["amount"] = format(_decimal(cost["amount"], field_name="usage.cost.amount"), "f")
                if not _known(cost.get("currency")) or not _known(cost.get("unit")):
                    raise TelemetryError("exact cost requires source currency and unit")
            usage["cost"] = cost
        event["usage"] = usage

    outcome = event.get("outcome")
    if outcome is not None:
        if not isinstance(outcome, Mapping):
            raise TelemetryError("outcome must be an object")
        outcome = dict(outcome)
        if "authoritative" in outcome and not isinstance(outcome["authoritative"], bool):
            raise TelemetryError("outcome.authoritative must be boolean")
        if "terminal" in outcome and not isinstance(outcome["terminal"], bool):
            raise TelemetryError("outcome.terminal must be boolean")
        event["outcome"] = outcome

    return event


def safe_append_event(path: str | os.PathLike[str], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Best-effort append that never raises into lifecycle code.

    The return value is telemetry health only. Callers must never use it as a lifecycle,
    Review, Integration, merge, or completion gate.
    """
    try:
        event = validate_event(raw)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        return {"telemetry_written": True, "degraded": False, "error": None}
    except Exception as exc:  # telemetry must not escape into lifecycle authority
        return {"telemetry_written": False, "degraded": True, "error": f"{type(exc).__name__}: {exc}"}


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
    attempts: set[Any] = field(default_factory=set)
    unknown_attempt_events: int = 0
    review_rounds: set[Any] = field(default_factory=set)
    review_ids: set[Any] = field(default_factory=set)
    review_phase_counts: Counter[str] = field(default_factory=Counter)
    operator_counts: Counter[str] = field(default_factory=Counter)
    operator_duration_ms: Counter[str] = field(default_factory=Counter)
    operator_duration_unknown: Counter[str] = field(default_factory=Counter)
    operator_action_ids: set[Any] = field(default_factory=set)
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
    terminal_outcomes: set[str] = field(default_factory=set)
    execution_values: dict[str, set[Any]] = field(default_factory=lambda: defaultdict(set))
    size_values: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))

    def add(self, event: Mapping[str, Any]) -> None:
        self.event_count += 1
        self.source_events.append({"source": event["source"], "source_event_id": event["source_event_id"]})
        for field_name, target in (
            ("task_id", self.task_ids),
            ("pr_number", self.pr_numbers),
            ("head_sha", self.heads),
            ("replaces_generation_id", self.replacement_ids),
        ):
            value = event.get(field_name, UNKNOWN)
            if _known(value):
                target.add(value)

        attempt = event.get("attempt_id", UNKNOWN)
        if _known(attempt):
            self.attempts.add(attempt)
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
        if operator.get("required") is True:
            category = operator["category"]
            self.operator_counts[category] += 1
            if _known(operator.get("action_id")):
                self.operator_action_ids.add(operator["action_id"])
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

        usage = event.get("usage") or {}
        for key in ("tool_calls", "input_tokens", "output_tokens", "total_tokens"):
            if _known(usage.get(key)):
                self.usage_sums[key] += int(usage[key])
                self.usage_known_events[key] += 1
            elif usage:
                self.usage_unknown_events[key] += 1
        cost = usage.get("cost") if usage else None
        if isinstance(cost, Mapping) and _known(cost.get("amount")) and _known(cost.get("currency")) and _known(cost.get("unit")):
            key = (str(cost["currency"]), str(cost["unit"]))
            self.cost_sums[key] += Decimal(str(cost["amount"]))
            self.cost_known_events += 1
        elif usage:
            self.cost_unknown_events += 1

        timing = event.get("timing") or {}
        stage = timing.get("stage") or event.get("event_type") or UNKNOWN
        if _known(timing.get("duration_ms")):
            self.stage_durations[str(stage)].append(int(timing["duration_ms"]))
        elif timing:
            self.unknown_stage_duration_events[str(stage)] += 1

        outcome = event.get("outcome") or {}
        if outcome.get("authoritative") is True and outcome.get("terminal") is True and _known(outcome.get("kind")):
            self.terminal_outcomes.add(str(outcome["kind"]))

        execution = event.get("execution") or {}
        if isinstance(execution, Mapping):
            for key in ("host", "provider", "model", "config", "snapshot"):
                if _known(execution.get(key)):
                    self.execution_values[key].add(execution[key])

        size = event.get("size") or {}
        if isinstance(size, Mapping):
            for key in ("files_changed", "additions", "deletions", "lines_changed"):
                if _known(size.get(key)):
                    self.size_values[key].add(_nonnegative_int(size[key], field_name=f"size.{key}"))

    def record(self) -> dict[str, Any]:
        terminal: Any
        if len(self.terminal_outcomes) == 1:
            terminal = next(iter(self.terminal_outcomes))
        else:
            terminal = UNKNOWN

        costs = [
            {"currency": currency, "unit": unit, "amount": format(amount, "f")}
            for (currency, unit), amount in sorted(self.cost_sums.items())
        ]
        repeated_gates = {gate: count for gate, count in sorted(self.override_gate_counts.items()) if count > 1}
        known_operator = sum(self.operator_counts.values())
        durations = {
            stage: {
                "known_ms": values,
                "unknown_event_count": self.unknown_stage_duration_events.get(stage, 0),
            }
            for stage, values in sorted(self.stage_durations.items())
        }
        for stage, unknown_count in sorted(self.unknown_stage_duration_events.items()):
            durations.setdefault(stage, {"known_ms": [], "unknown_event_count": unknown_count})

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
                "exact_ids": sorted(self.attempts, key=str),
                "exact_count": len(self.attempts),
                "unknown_event_count": self.unknown_attempt_events,
            },
            "review": {
                "round_ids": sorted(self.review_rounds, key=str),
                "round_count": len(self.review_rounds),
                "review_ids": sorted(self.review_ids, key=str),
                "phase_counts": dict(sorted(self.review_phase_counts.items())),
            },
            "operator": {
                "intervention_count": known_operator,
                "category_counts": dict(sorted(self.operator_counts.items())),
                "category_duration_ms": dict(sorted(self.operator_duration_ms.items())),
                "category_duration_unknown_events": dict(sorted(self.operator_duration_unknown.items())),
                "action_ids": sorted(self.operator_action_ids, key=str),
                "override_count": sum(self.override_gate_counts.values()) + self.unknown_override_gate_events,
                "override_gate_counts": dict(sorted(self.override_gate_counts.items())),
                "repeated_same_gate": repeated_gates,
                "unknown_override_gate_events": self.unknown_override_gate_events,
            },
            "usage": {
                key: {
                    "attributed_value": self.usage_sums.get(key, 0),
                    "known_event_count": self.usage_known_events.get(key, 0),
                    "unknown_event_count": self.usage_unknown_events.get(key, 0),
                }
                for key in ("tool_calls", "input_tokens", "output_tokens", "total_tokens")
            }
            | {
                "cost": {
                    "exact_source_units": costs,
                    "known_event_count": self.cost_known_events,
                    "unknown_event_count": self.cost_unknown_events,
                }
            },
            "timing": {"stages": durations},
            "execution": {key: sorted(values, key=str) for key, values in sorted(self.execution_values.items())},
            "size": {key: sorted(values) for key, values in sorted(self.size_values.items())},
            "terminal_outcome": terminal,
            "terminal_outcome_conflict": sorted(self.terminal_outcomes) if len(self.terminal_outcomes) > 1 else [],
        }


def collect(events: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect idempotent generation records and advisory diagnostics."""
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

        lineage = event["lineage_id"]
        generation = event["generation_id"]
        if _known(lineage) and _known(generation):
            group_key = (event["repository"], event["series"], lineage, generation)
        else:
            # Unknown lifecycle identity must not collapse unrelated observations.
            group_key = (event["repository"], event["series"], UNKNOWN, event["source"], event["source_event_id"])
        if group_key not in groups:
            groups[group_key] = GenerationAccumulator(
                repository=event["repository"],
                series=event["series"],
                lineage_id=lineage if _known(lineage) else UNKNOWN,
                generation_id=generation if _known(generation) else UNKNOWN,
            )
        groups[group_key].add(event)

    records = [group.record() for _, group in sorted(groups.items(), key=lambda item: tuple(map(str, item[0])))]
    report = build_report(records, duplicate_events=duplicate_events, repository_health_events=repository_health_events)
    return records, report


def _percentile(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def build_report(
    records: Sequence[Mapping[str, Any]],
    *,
    duplicate_events: int = 0,
    repository_health_events: int = 0,
    low_sample_threshold: int = 5,
) -> dict[str, Any]:
    """Build diagnostic-only count/p50/p90 summaries; never a routing score or gate."""
    stages: dict[str, list[int]] = defaultdict(list)
    stage_unknowns: Counter[str] = Counter()
    pr_flow_records = [record for record in records if record.get("series") == PR_FLOW]
    for record in pr_flow_records:
        for stage, payload in (record.get("timing", {}).get("stages", {}) or {}).items():
            stages[stage].extend(payload.get("known_ms", []))
            stage_unknowns[stage] += int(payload.get("unknown_event_count", 0))

    timing = {}
    for stage in sorted(set(stages) | set(stage_unknowns)):
        values = stages.get(stage, [])
        count = len(values)
        timing[stage] = {
            "count": count,
            "unknown_event_count": stage_unknowns.get(stage, 0),
            "p50_ms": _percentile(values, 0.50),
            "p90_ms": _percentile(values, 0.90),
            "low_sample": count < low_sample_threshold,
        }

    return {
        "schema_version": "lifecycle-economics-diagnostic-report/v1",
        "authority": "diagnostic_only",
        "generation_count": len(records),
        "pr_flow_generation_count": len(pr_flow_records),
        "repository_health_generation_count": len(records) - len(pr_flow_records),
        "repository_health_event_count": repository_health_events,
        "deduplicated_source_event_count": duplicate_events,
        "timing": timing,
        "low_sample_threshold": low_sample_threshold,
        "eligibility": UNKNOWN,
        "routing_recommendation": UNKNOWN,
    }


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
    with Path(args.event_json).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        result = {"telemetry_written": False, "degraded": True, "error": "TelemetryError: event JSON must be an object"}
    else:
        result = safe_append_event(args.output, raw)
    print(json.dumps(result, sort_keys=True))
    # Deliberately zero: telemetry degradation is not lifecycle failure.
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="derive observational records and diagnostics from JSONL events")
    collect_parser.add_argument("--input", required=True)
    collect_parser.add_argument("--records", required=True)
    collect_parser.add_argument("--report", required=True)
    collect_parser.set_defaults(func=_cmd_collect)

    emit_parser = subparsers.add_parser("emit", help="best-effort append one telemetry event without lifecycle authority")
    emit_parser.add_argument("--event-json", required=True)
    emit_parser.add_argument("--output", required=True)
    emit_parser.set_defaults(func=_cmd_emit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
