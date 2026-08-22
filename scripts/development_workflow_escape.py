#!/usr/bin/env python3
"""Validate, render, parse, and fold Development Workflow escape records.

The v1 ledger is append-only evidence. This helper is deliberately read-only: it
never writes Asana, GitHub, task notes, lifecycle state, priority, Review, or merge
state. Missing evidence remains the literal string UNKNOWN.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "dish-development-workflow-escape:v1"
MARKER = f"<!-- {SCHEMA} -->"
UNKNOWN = "UNKNOWN"
ROOT_CLASSES = {
    "human-review miss",
    "operator-surface blindness",
    "platform-constraint blindness",
    "authority/projection confusion",
    "self-deadlock",
    "partial-outcome masking",
    "activation/runtime gap",
    "test gap",
    "scope drift",
    "other-explicit",
}
TOP_LEVEL_FIELDS = {
    "schema",
    "escape_id",
    "observed_at",
    "affected_change",
    "observed_failure",
    "discovery_evidence",
    "design_named_failure_mode",
    "marco_approval",
    "design_review",
    "code_review_testing",
    "implementation_vs_reviewed_design",
    "activation_runtime",
    "operator_impact",
    "root_class",
    "corrective_owner",
    "owning_boundary",
    "telemetry_evidence",
}

_GITHUB_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_AFFECTED_CHANGE_RE = re.compile(
    rf"^github:{_GITHUB_REPO}:(?:pr:[1-9][0-9]*@[0-9a-f]{{40}}|commit:[0-9a-f]{{40}})$"
)
_EVIDENCE_RES = (
    re.compile(rf"^github:{_GITHUB_REPO}:review:[1-9][0-9]*@[0-9a-f]{{40}}$"),
    re.compile(rf"^github:{_GITHUB_REPO}:run:[1-9][0-9]*@[0-9a-f]{{40}}$"),
    re.compile(rf"^github:{_GITHUB_REPO}:comment:[1-9][0-9]*$"),
    re.compile(r"^asana:story:[1-9][0-9]*$"),
    re.compile(r"^asana:task:[1-9][0-9]*$"),
    re.compile(r"^runtime:[A-Za-z0-9._/-]+:[A-Za-z0-9._:@/-]+$"),
)
_DECISION_RE = re.compile(
    r"^asana:decision:[A-Za-z0-9._-]+@story:[1-9][0-9]*$"
)
_CORRECTIVE_OWNER_RE = re.compile(r"^asana:task:[1-9][0-9]*$")
_TELEMETRY_RE = re.compile(r"^telemetry:[A-Za-z0-9._/-]+:[A-Za-z0-9._:@/-]+$")
_ESCAPE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DECIMAL_AMOUNT_RE = re.compile(r"^(0|[0-9]+)(\.[0-9]+)?$")


class EscapeRecordError(ValueError):
    """A marker or record is malformed, ambiguous, or source-invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value != UNKNOWN


def _validate_exact_evidence(value: str) -> None:
    if not isinstance(value, str) or not any(pattern.fullmatch(value) for pattern in _EVIDENCE_RES):
        raise EscapeRecordError(f"invalid exact evidence identity: {value!r}")


def escape_identity(*, affected_change: str, discovery_evidence: Sequence[str]) -> str:
    if not _AFFECTED_CHANGE_RE.fullmatch(str(affected_change)):
        raise EscapeRecordError("affected_change must be an exact GitHub PR-head or commit identity")
    if not isinstance(discovery_evidence, Sequence) or isinstance(discovery_evidence, (str, bytes)):
        raise EscapeRecordError("discovery_evidence must be a non-empty sequence")
    evidence = list(discovery_evidence)
    if not evidence:
        raise EscapeRecordError("discovery_evidence must contain exact source evidence")
    if len(evidence) != len(set(evidence)):
        raise EscapeRecordError("discovery_evidence contains duplicate identities")
    for item in evidence:
        _validate_exact_evidence(item)
    identity_material = {
        "affected_change": affected_change,
        "discovery_evidence": sorted(evidence),
    }
    digest = hashlib.sha256(_canonical_json(identity_material).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _validate_observed_at(value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EscapeRecordError("observed_at must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EscapeRecordError("observed_at is not a valid RFC3339 timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise EscapeRecordError("observed_at must be UTC")


def _validate_gap(name: str, value: Any, *, statuses: set[str]) -> None:
    if not isinstance(value, Mapping) or set(value) != {"status", "detail"}:
        raise EscapeRecordError(f"{name} must contain exactly status and detail")
    status = value["status"]
    detail = value["detail"]
    if status not in statuses:
        raise EscapeRecordError(f"{name}.status is invalid")
    if status == UNKNOWN:
        if detail != UNKNOWN:
            raise EscapeRecordError(f"{name}.detail must remain UNKNOWN when status is UNKNOWN")
    elif not _is_nonempty_text(detail):
        raise EscapeRecordError(f"{name}.detail must be exact non-UNKNOWN text when status is known")


def _validate_marco_approval(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {"status", "decision_evidence"}:
        raise EscapeRecordError("marco_approval must contain exactly status and decision_evidence")
    status = value["status"]
    evidence = value["decision_evidence"]
    if status not in {"YES", "NO", "NOT_APPLICABLE", UNKNOWN}:
        raise EscapeRecordError("marco_approval.status is invalid")
    if status in {"YES", "NO"}:
        if not isinstance(evidence, str) or not _DECISION_RE.fullmatch(evidence):
            raise EscapeRecordError(
                "Marco approval/refusal requires an explicit durable decision identity; account attribution is not decision provenance"
            )
    elif evidence != UNKNOWN:
        raise EscapeRecordError("marco_approval.decision_evidence must be UNKNOWN when approval is absent or not applicable")


def _validate_operator_impact(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {"duration_ms", "cost"}:
        raise EscapeRecordError("operator_impact must contain exactly duration_ms and cost")
    duration = value["duration_ms"]
    if duration != UNKNOWN and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 0):
        raise EscapeRecordError("operator_impact.duration_ms must be a non-negative exact integer or UNKNOWN")
    cost = value["cost"]
    if cost == UNKNOWN:
        return
    if not isinstance(cost, Mapping) or set(cost) != {"amount", "currency", "unit"}:
        raise EscapeRecordError("operator_impact.cost must be UNKNOWN or exact amount/currency/unit")
    if not all(_is_nonempty_text(cost.get(key)) for key in ("amount", "currency", "unit")):
        raise EscapeRecordError("operator_impact.cost fields must be non-empty exact strings")
    if not _DECIMAL_AMOUNT_RE.fullmatch(cost["amount"]):
        raise EscapeRecordError("operator_impact.cost.amount must be an exact decimal string")
    try:
        amount = Decimal(cost["amount"])
    except InvalidOperation as exc:
        raise EscapeRecordError("operator_impact.cost.amount must be an exact decimal string") from exc
    if amount < 0 or not amount.is_finite():
        raise EscapeRecordError("operator_impact.cost.amount must be a finite non-negative value")


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise EscapeRecordError("escape record must be a JSON object")
    keys = set(record)
    missing = TOP_LEVEL_FIELDS - keys
    unknown = keys - TOP_LEVEL_FIELDS
    if missing:
        raise EscapeRecordError(f"escape record missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise EscapeRecordError(f"escape record has unknown fields: {', '.join(sorted(unknown))}")
    if record["schema"] != SCHEMA:
        raise EscapeRecordError(f"schema must be exactly {SCHEMA}")
    if not isinstance(record["escape_id"], str) or not _ESCAPE_ID_RE.fullmatch(record["escape_id"]):
        raise EscapeRecordError("escape_id must be sha256:<64 lowercase hex>")
    _validate_observed_at(record["observed_at"])
    if not _AFFECTED_CHANGE_RE.fullmatch(str(record["affected_change"])):
        raise EscapeRecordError("affected_change must be an exact GitHub PR-head or commit identity")
    if not _is_nonempty_text(record["observed_failure"]):
        raise EscapeRecordError("observed_failure must be exact non-UNKNOWN text")

    discovery = record["discovery_evidence"]
    if not isinstance(discovery, list) or not discovery:
        raise EscapeRecordError("discovery_evidence must be a non-empty array")
    if len(discovery) != len(set(discovery)):
        raise EscapeRecordError("discovery_evidence contains duplicate identities")
    for item in discovery:
        _validate_exact_evidence(item)

    expected_id = escape_identity(
        affected_change=record["affected_change"], discovery_evidence=discovery
    )
    if record["escape_id"] != expected_id:
        raise EscapeRecordError(
            f"escape_id does not match deterministic exact-evidence identity; expected {expected_id}"
        )

    if record["design_named_failure_mode"] not in {"YES", "NO", UNKNOWN}:
        raise EscapeRecordError("design_named_failure_mode must be YES, NO, or UNKNOWN")
    _validate_marco_approval(record["marco_approval"])
    _validate_gap("design_review", record["design_review"], statuses={"MISSED", "NOT_MISSED", UNKNOWN})
    _validate_gap(
        "code_review_testing",
        record["code_review_testing"],
        statuses={"MISSED", "NOT_MISSED", UNKNOWN},
    )
    _validate_gap(
        "implementation_vs_reviewed_design",
        record["implementation_vs_reviewed_design"],
        statuses={"DRIFT", "ALIGNED", UNKNOWN},
    )
    _validate_gap(
        "activation_runtime",
        record["activation_runtime"],
        statuses={"GAP", "NO_GAP", UNKNOWN},
    )
    _validate_operator_impact(record["operator_impact"])

    if record["root_class"] not in ROOT_CLASSES:
        raise EscapeRecordError("root_class is not in the closed v1 escape-class set")
    owner = record["corrective_owner"]
    if owner != "UNOWNED" and (not isinstance(owner, str) or not _CORRECTIVE_OWNER_RE.fullmatch(owner)):
        raise EscapeRecordError("corrective_owner must be an exact asana:task:<gid> identity or UNOWNED")
    if not _is_nonempty_text(record["owning_boundary"]):
        raise EscapeRecordError("owning_boundary must be exact non-UNKNOWN text")

    telemetry = record["telemetry_evidence"]
    if telemetry != UNKNOWN:
        if not isinstance(telemetry, list) or not telemetry:
            raise EscapeRecordError("telemetry_evidence must be UNKNOWN or a non-empty array")
        if len(telemetry) != len(set(telemetry)):
            raise EscapeRecordError("telemetry_evidence contains duplicate identities")
        if not all(isinstance(item, str) and _TELEMETRY_RE.fullmatch(item) for item in telemetry):
            raise EscapeRecordError("telemetry_evidence accepts only exact telemetry:<source>:<id> identities")

    return dict(record)


def render_comment(record: Mapping[str, Any]) -> str:
    validated = validate_record(record)
    return f"{MARKER}\n{_canonical_json(validated)}"


def parse_comment(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str):
        raise EscapeRecordError("comment text must be a string")
    if MARKER not in text:
        return None
    stripped = text.strip()
    prefix = MARKER + "\n"
    if not stripped.startswith(prefix):
        raise EscapeRecordError("escape marker must be the first line of the comment")
    payload = stripped[len(prefix):]
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EscapeRecordError("escape comment payload must be exactly one JSON object") from exc
    return validate_record(value)


def records_from_stories(stories: Iterable[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for story in stories:
        if isinstance(story, str):
            text = story
        elif isinstance(story, Mapping):
            text = story.get("text")
            if text is None:
                text = story.get("body")
            if text is None:
                continue
        else:
            raise EscapeRecordError("story entries must be strings or objects with text/body")
        parsed = parse_comment(str(text))
        if parsed is not None:
            records.append(parsed)
    return records


def _dedupe(records: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    canonical: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    duplicate_count = 0
    for raw in records:
        record = validate_record(raw)
        encoded = _canonical_json(record)
        prior = by_id.get(record["escape_id"])
        if prior is None:
            by_id[record["escape_id"]] = encoded
            canonical.append(record)
        elif prior == encoded:
            duplicate_count += 1
        else:
            raise EscapeRecordError(
                f"conflicting records share exact escape identity {record['escape_id']}"
            )
    return canonical, duplicate_count


def _impact_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "escape_id": record["escape_id"],
        "observed_at": record["observed_at"],
        "root_class": record["root_class"],
        "corrective_owner": record["corrective_owner"],
        "duration_ms": record["operator_impact"]["duration_ms"],
        "cost": record["operator_impact"]["cost"],
    }


def fold_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    raw = list(records)
    canonical, duplicate_count = _dedupe(raw)

    root_counts = Counter(record["root_class"] for record in canonical)
    safeguards = Counter()
    for record in canonical:
        if record["design_named_failure_mode"] == "NO":
            safeguards["design-failure-mode-not-named"] += 1
        if record["design_review"]["status"] == "MISSED":
            safeguards["design-review"] += 1
        if record["code_review_testing"]["status"] == "MISSED":
            safeguards["code-review-testing"] += 1
        if record["implementation_vs_reviewed_design"]["status"] == "DRIFT":
            safeguards["implementation-vs-reviewed-design"] += 1
        if record["activation_runtime"]["status"] == "GAP":
            safeguards["activation-runtime"] += 1

    owned: Counter[str] = Counter()
    unowned: list[str] = []
    duration_values: list[int] = []
    unknown_duration_count = 0
    costs: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    cost_counts: Counter[tuple[str, str]] = Counter()
    unknown_cost_count = 0
    trend: list[dict[str, Any]] = []
    exact_duration_examples: list[Mapping[str, Any]] = []

    for record in canonical:
        owner = record["corrective_owner"]
        if owner == "UNOWNED":
            unowned.append(record["escape_id"])
        else:
            owned[owner] += 1

        impact = record["operator_impact"]
        duration = impact["duration_ms"]
        if duration == UNKNOWN:
            unknown_duration_count += 1
        else:
            duration_values.append(duration)
            exact_duration_examples.append(record)
        cost = impact["cost"]
        if cost == UNKNOWN:
            unknown_cost_count += 1
        else:
            key = (cost["currency"], cost["unit"])
            costs[key] += Decimal(cost["amount"])
            cost_counts[key] += 1

        telemetry = record["telemetry_evidence"]
        unknown_dimensions = []
        for name, value in (
            ("design_named_failure_mode", record["design_named_failure_mode"]),
            ("design_review", record["design_review"]["status"]),
            ("code_review_testing", record["code_review_testing"]["status"]),
            ("implementation_vs_reviewed_design", record["implementation_vs_reviewed_design"]["status"]),
            ("activation_runtime", record["activation_runtime"]["status"]),
            ("operator_duration", duration),
            ("operator_cost", cost),
            ("telemetry_evidence", telemetry),
            ("marco_approval", record["marco_approval"]["status"]),
        ):
            if value == UNKNOWN:
                unknown_dimensions.append(name)
        trend.append(
            {
                "escape_id": record["escape_id"],
                "observed_at": record["observed_at"],
                "exact_source_identity_count": 1 + len(record["discovery_evidence"]),
                "telemetry_reference_count": UNKNOWN if telemetry == UNKNOWN else len(telemetry),
                "operator_duration_ms": duration,
                "operator_cost": cost,
                "unknown_dimensions": sorted(unknown_dimensions),
            }
        )

    # Prefer the largest exact duration; for equal duration prefer the more recent
    # occurrence.  The final escape-id tie-breaker keeps output deterministic.
    exact_duration_examples.sort(key=lambda record: record["escape_id"])
    exact_duration_examples.sort(key=lambda record: record["observed_at"], reverse=True)
    exact_duration_examples.sort(
        key=lambda record: record["operator_impact"]["duration_ms"], reverse=True
    )
    trend.sort(key=lambda item: (item["observed_at"], item["escape_id"]))
    digest_material = [_canonical_json(record) for record in canonical]
    input_digest = hashlib.sha256("\n".join(digest_material).encode("utf-8")).hexdigest()

    return {
        "schema": "dish-development-workflow-escape-report:v1",
        "authority": "diagnostic_only",
        "writes_performed": False,
        "parent_task_notes_mutated": False,
        "eligibility": UNKNOWN,
        "routing_recommendation": UNKNOWN,
        "priority_change": UNKNOWN,
        "review_change": UNKNOWN,
        "merge_change": UNKNOWN,
        "human_gate_change": UNKNOWN,
        "input_record_count": len(raw),
        "canonical_record_count": len(canonical),
        "deduplicated_exact_evidence_count": duplicate_count,
        "canonical_input_digest": f"sha256:{input_digest}",
        "active_root_classes": [
            {"root_class": key, "recurrence_count": root_counts[key]}
            for key in sorted(root_counts)
        ],
        "recent_high_impact_examples": [
            _impact_summary(record) for record in exact_duration_examples[:5]
        ],
        "repeatedly_failing_safeguards": [
            {"safeguard": key, "recurrence_count": safeguards[key]}
            for key in sorted(safeguards)
            if safeguards[key] >= 2
        ],
        "corrective_owner_state": {
            "owned_escape_count": sum(owned.values()),
            "unowned_escape_count": len(unowned),
            "owners": [
                {"task": task, "escape_count": owned[task], "task_state": UNKNOWN}
                for task in sorted(owned)
            ],
            "unowned_escape_ids": sorted(unowned),
        },
        "operator_impact": {
            "exact_duration_count": len(duration_values),
            "unknown_duration_count": unknown_duration_count,
            "total_exact_duration_ms": sum(duration_values),
            "exact_costs": [
                {
                    "currency": currency,
                    "unit": unit,
                    "event_count": cost_counts[(currency, unit)],
                    "amount": format(costs[(currency, unit)], "f"),
                }
                for currency, unit in sorted(costs)
            ],
            "unknown_cost_count": unknown_cost_count,
        },
        "evidence_operator_impact_trend": trend,
    }


def report_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    report = fold_records(records)
    return (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EscapeRecordError(f"cannot read JSON from {path}: {exc}") from exc


def _cmd_validate(args: argparse.Namespace) -> int:
    record = validate_record(_load_json(args.record_json))
    print(json.dumps({"valid": True, "escape_id": record["escape_id"]}, sort_keys=True))
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    print(render_comment(_load_json(args.record_json)))
    return 0


def _cmd_fold(args: argparse.Namespace) -> int:
    stories = _load_json(args.stories_json)
    if not isinstance(stories, list):
        raise EscapeRecordError("stories JSON must be an array")
    print(report_bytes(records_from_stories(stories)).decode("utf-8"), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate one record JSON object")
    validate.add_argument("--record-json", type=Path, required=True)
    validate.set_defaults(func=_cmd_validate)
    render = sub.add_parser("render", help="render one canonical append-only Asana comment")
    render.add_argument("--record-json", type=Path, required=True)
    render.set_defaults(func=_cmd_render)
    fold = sub.add_parser("fold", help="parse/fold an exported array of Asana stories/comments")
    fold.add_argument("--stories-json", type=Path, required=True)
    fold.set_defaults(func=_cmd_fold)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EscapeRecordError as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
