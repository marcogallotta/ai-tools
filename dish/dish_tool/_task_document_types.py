"""Canonical Dish task-document value, finding, and rendering types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from ._task_document_syntax import (
    MATERIAL_CHANGE_ACCEPTED_SYNTAX,
    PLANNING_FIELDS,
    RESEARCH_BASIS_PREFIXES,
    SECTION_ORDER,
    STATE_FIELDS,
)


class FindingKind(str, Enum):
    SYNTAX = "syntax"
    ILLEGAL_COMBINATION = "illegal-combination"
    AGENT_CORRECTABLE = "agent-correctable"
    SEMANTIC_REVIEW = "semantic-review"
    SCHEMA_VERSION = "schema-version"


@dataclass(frozen=True)
class RecoverySpec:
    expected: Any | None = None
    example: Any | None = None
    recovery: str | None = None


RECOVERY_SPECS: Mapping[str, RecoverySpec] = {
    "document.recognition-empty": RecoverySpec(
        expected={
            "line": 2,
            "syntax": "<what the dish is, how it eats, and its meal role>",
        },
        example=[
            "Dish name — short identity phrase",
            "A concise sentence describing what it is, how it eats, and its meal role.",
        ],
        recovery="Insert one non-empty dish-summary sentence immediately after the title line.",
    ),
    "document.required-section": RecoverySpec(
        expected="A non-empty required canonical section with the reported heading.",
        recovery="Insert the missing required section in canonical section order and populate it with non-empty content.",
    ),
    "quantities.portions-required": RecoverySpec(
        expected="Portions: <non-empty serving count or yield>",
        example="Portions: 2",
        recovery="Add or complete a non-empty `Portions:` line inside QUANTITIES.",
    ),
    "planning.field-empty": RecoverySpec(
        expected="<field name>: <non-empty value>",
        recovery="Populate the reported Planning brief field with a non-empty value.",
    ),
    "decisions.human-format": RecoverySpec(
        expected="Human — Marco: <decision text>",
        example="Human — Marco: Approved the scoped nutrition exemptions for this controlled tasting repeat.",
        recovery="Rewrite the Decision entry using the exact `Human — Marco:` prefix followed by non-empty decision text.",
    ),
    "state.actor-format": RecoverySpec(
        expected="<agent>, <self-reported model: model>, <YYYY-MM-DD>",
        example="Claude, self-reported model: Claude, 2026-08-03",
        recovery="Rewrite the reported actor field using the canonical actor, model, and date grammar.",
    ),
    "research-basis.classification": RecoverySpec(
        expected={"prefix": "Classification:", "allowed_values": list(RESEARCH_BASIS_PREFIXES)},
        recovery="Add or correct one Research basis line so `Classification:` begins with an approved classification value.",
    ),
    "title.recognition": RecoverySpec(
        expected="<dish name> — <short identity phrase>",
        example="Laap gai — controlled chicken-thigh pork-replacement repeat",
        recovery="Rewrite line 1 so the dish name and short identity phrase are separated by a spaced em dash (` — `).",
    ),
    "state.illegal-combination": RecoverySpec(
        expected="State fields must match the canonical combination for the current Status.",
        recovery="Correct only the conflicting PROCESS RECORD state fields to the canonical combination for the reported Status.",
    ),
    "role.title-brief-disagreement": RecoverySpec(
        expected="The title role tag and Planning brief `Role` must describe the same meal role.",
        recovery="Make the title role tag and Planning brief `Role` agree; changing the governed Planning field still requires authorization.",
    ),
    "title.destination-marker": RecoverySpec(
        expected="The title and Planning brief `Destination section` must contain the same destination-defect marker, or neither may contain one.",
        recovery="Make the destination marker agree between the title and Planning brief without changing the intended destination.",
    ),
    "material-changes.format": RecoverySpec(
        expected=MATERIAL_CHANGE_ACCEPTED_SYNTAX,
        recovery="Rewrite the complete Material changes entry using the canonical seven-field grammar.",
    ),
}


@dataclass(frozen=True)
class DocumentFinding:
    rule: str
    kind: FindingKind
    message: str
    location: Any | None = None
    current: Any | None = None
    expected: Any | None = None
    example: Any | None = None
    recovery: str | None = None
    related: Mapping[str, Any] | None = None


def _default_recovery(finding: DocumentFinding) -> str | None:
    if finding.kind is FindingKind.SYNTAX:
        return "Correct the reported canonical syntax and rerun deterministic validation."
    if finding.kind is FindingKind.AGENT_CORRECTABLE:
        return "Correct the reported candidate defect without changing human-owned intent, then rerun deterministic validation."
    if finding.kind is FindingKind.ILLEGAL_COMBINATION:
        return "Correct the conflicting values so they form one legal canonical combination, then rerun deterministic validation."
    return None


def finding_payload(finding: DocumentFinding) -> dict[str, Any]:
    """Serialize a finding with deterministic repair guidance when agent-correctable."""
    spec = RECOVERY_SPECS.get(finding.rule)
    payload: dict[str, Any] = {
        "rule": finding.rule,
        "kind": finding.kind.value,
        "message": finding.message,
        "location": finding.location,
        "current": finding.current,
    }
    optional = {
        "expected": finding.expected if finding.expected is not None else (spec.expected if spec else None),
        "example": finding.example if finding.example is not None else (spec.example if spec else None),
        "recovery": finding.recovery if finding.recovery is not None else (spec.recovery if spec else _default_recovery(finding)),
        "related": dict(finding.related) if finding.related is not None else None,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


@dataclass(frozen=True)
class DocumentValidation:
    findings: tuple[DocumentFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings

    def by_kind(self, kind: FindingKind) -> tuple[DocumentFinding, ...]:
        return tuple(item for item in self.findings if item.kind is kind)


@dataclass(frozen=True)
class PlanningBrief:
    values: Mapping[str, str]

    def render(self, *, heading: bool = False) -> str:
        lines = ["### Planning brief"] if heading else []
        lines.extend(f"{name}: {self.values[name]}" for name in PLANNING_FIELDS)
        return "\n".join(lines)


@dataclass(frozen=True)
class TaskState:
    values: Mapping[str, str]

    def render(self) -> str:
        return "\n".join(f"{name}: {self.values[name]}" for name in STATE_FIELDS)


@dataclass(frozen=True)
class CanonicalTaskDocument:
    title: str
    recognition: str
    introduction: tuple[str, ...]
    sections: Mapping[str, str]
    state: TaskState
    planning_brief: PlanningBrief
    decisions: tuple[str, ...]
    research_basis: tuple[str, ...]
    material_changes: tuple[str, ...]
    schema_version: str

    @property
    def is_non_main(self) -> bool:
        return self.title.startswith("[non-main]")

    @property
    def nutrition_scope(self) -> str:
        return "out-of-scope" if self.is_non_main else "main"

    def render(self) -> str:
        lines = [self.title, self.recognition]
        lines.extend(self.introduction)
        for name in SECTION_ORDER:
            if name not in self.sections:
                continue
            heading = name if name == "WHY COOK IT" else f"## {name}"
            lines.extend([heading, self.sections[name]])
        lines.extend(["---", "## PROCESS RECORD", self.state.render(), self.planning_brief.render(heading=True)])
        if self.decisions:
            lines.append("### Decisions")
            lines.extend(self.decisions)
        lines.append("### Research basis")
        lines.extend(self.research_basis)
        if self.material_changes:
            lines.append("### Material changes")
            lines.extend(self.material_changes)
        lines.append(f"Schema version: {self.schema_version}")
        return "\n".join(lines).rstrip() + "\n"


class DocumentParseError(ValueError):
    def __init__(
        self,
        rule: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
        errors: Sequence[Mapping[str, object]] | None = None,
    ):
        super().__init__(message)
        self.rule = rule
        self.details = dict(details or {})
        self.errors = tuple(dict(item) for item in (errors or ()))


def document_parse_error_payloads(exc: DocumentParseError) -> list[dict[str, object]]:
    if exc.errors:
        return [dict(item) for item in exc.errors]
    payload: dict[str, object] = {"rule": exc.rule, "message": str(exc)}
    payload.update(exc.details)
    return [payload]
