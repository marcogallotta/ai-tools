"""Deterministic parser, renderer, and validator for canonical dish task bodies."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence

PLANNING_FIELDS = (
    "Dish candidate", "Purpose", "Role", "Priors", "Locks", "Exemptions",
    "Research emphasis", "Destination section",
)
STATE_FIELDS = (
    "Status", "Status detail", "Resume status", "Verification protocol release",
    "Researched by", "Verified by", "Self-verified",
)
REQUIRED_SECTIONS = (
    "WHAT TO BUY", "QUANTITIES", "HOW TO COOK IT", "WHAT SUCCESS LOOKS LIKE",
)
OPTIONAL_SECTIONS = ("WHY COOK IT", "CHECK BEFORE COOKING", "WATCH OUT FOR", "STORAGE")
SECTION_ORDER = ("WHY COOK IT", "WHAT TO BUY", "CHECK BEFORE COOKING", "QUANTITIES", "HOW TO COOK IT", "WHAT SUCCESS LOOKS LIKE", "WATCH OUT FOR", "STORAGE")
ALLOWED_SECTIONS = frozenset(SECTION_ORDER)
TOP_LEVEL_HEADINGS = tuple(
    name if name == "WHY COOK IT" else f"## {name}"
    for name in SECTION_ORDER
)
PROCESS_HEADING = "## PROCESS RECORD"
PROCESS_SUBHEADINGS = (
    "### Planning brief",
    "### Decisions",
    "### Research basis",
    "### Material changes",
)
ALLOWED_STATUSES = frozenset(
    {"pending-research", "pending-evidence", "pending-human-review", "pending-verification", "ready"}
)
RESEARCH_BASIS_PREFIXES = (
    "Source-backed dish", "Halal port of ", "Intentional test dish, human-approved",
)
DESTINATION_RE = re.compile(r"^(?P<name>.+?)\s+—\s+(?P<gid>[0-9]+)$")
ACTOR_NAME_PATTERN = r"(?:ChatGPT|Custom GPT|Claude|Codex)"
MODEL_PATTERN = r"[^,—]+"
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
ACTOR_RE = re.compile(
    rf"^{ACTOR_NAME_PATTERN} — {MODEL_PATTERN}, {DATE_PATTERN}$"
)
MATERIAL_CHANGE_RE = re.compile(
    rf"^{DATE_PATTERN} — {ACTOR_NAME_PATTERN} — {MODEL_PATTERN} — "
    rf".+ — .+ — (?:Small|Large) — "
    rf"(?:pending-verification|verified — {ACTOR_NAME_PATTERN}, {MODEL_PATTERN}, {DATE_PATTERN})$"
)
MATERIAL_CHANGE_ACCEPTED_SYNTAX = (
    "<YYYY-MM-DD> — <ChatGPT|Custom GPT|Claude|Codex> — "
    "<self-reported model: model> — "
    "<change> — <reason> — <Small|Large> — "
    "<pending-verification|verified — <agent>, <self-reported model: model>, "
    "<YYYY-MM-DD>>"
)


class FindingKind(str, Enum):
    SYNTAX = "syntax"
    ILLEGAL_COMBINATION = "illegal-combination"
    AGENT_CORRECTABLE = "agent-correctable"
    SEMANTIC_REVIEW = "semantic-review"
    SCHEMA_VERSION = "schema-version"


@dataclass(frozen=True)
class DocumentFinding:
    rule: str
    kind: FindingKind
    message: str
    location: str | None = None




def finding_payload(finding: DocumentFinding) -> dict[str, str | None]:
    """Serialize a validation finding without discarding actionable context."""
    return {
        "rule": finding.rule,
        "kind": finding.kind.value,
        "message": finding.message,
        "location": finding.location,
    }


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


def _authority_label_has_format_characters(label: str) -> bool:
    return any(unicodedata.category(character) == "Cf" for character in label)


def _normalized_authority_label(label: str) -> str:
    without_format_characters = "".join(
        character
        for character in label
        if unicodedata.category(character) != "Cf"
    )
    normalized = unicodedata.normalize("NFKC", without_format_characters)
    return " ".join(normalized.split()).casefold()


def _canonical_authority_label(label: str, names: Sequence[str]) -> str | None:
    normalized = _normalized_authority_label(label)
    return next(
        (name for name in names if _normalized_authority_label(name) == normalized),
        None,
    )


def _authority_field_match(line: str) -> tuple[re.Match[str] | None, bool]:
    """Return an ASCII-colon field match and whether compatibility folding was required."""
    match = re.match(r"^([^:]+):(?:\s*(.*))$", line)
    if match is not None:
        return match, False
    normalized = unicodedata.normalize("NFKC", line)
    if normalized == line:
        return None, False
    return re.match(r"^([^:]+):(?:\s*(.*))$", normalized), True


def _field_label_errors(
    lines: Sequence[str],
    names: Sequence[str],
    *,
    context: str,
    line_numbers: Sequence[int],
) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    for line, line_number in zip(lines, line_numbers):
        match, compatibility_folded = _authority_field_match(line)
        if match is None:
            continue
        label = match.group(1)
        canonical = _canonical_authority_label(label, names)
        if compatibility_folded and canonical is not None:
            errors.append(
                {
                    "rule": f"{context}_field_label_disguised",
                    "field": canonical,
                    "canonical_label": canonical,
                    "line": line_number,
                    "message": (
                        f"{context} field {canonical} uses Unicode compatibility syntax"
                    ),
                }
            )
            continue
        has_format_characters = _authority_label_has_format_characters(label)
        if has_format_characters:
            error: dict[str, object] = {
                "rule": f"{context}_field_label_format_character",
                "label": label,
                "line": line_number,
                "message": (
                    f"{context} field labels must not contain Unicode format characters"
                ),
            }
            if canonical is not None:
                error["field"] = canonical
                error["canonical_label"] = canonical
            errors.append(error)
            continue
        if canonical is None or label == canonical:
            continue
        errors.append(
            {
                "rule": f"{context}_field_label_noncanonical",
                "field": canonical,
                "label": label,
                "canonical_label": canonical,
                "line": line_number,
                "message": (
                    f"non-canonical {context} field label {label}; "
                    f"use {canonical}"
                ),
            }
        )
    return errors


def _canonical_authority_heading(
    heading: str, canonical_headings: Sequence[str]
) -> str | None:
    normalized = _normalized_authority_label(heading)
    return next(
        (
            canonical
            for canonical in canonical_headings
            if _normalized_authority_label(canonical) == normalized
        ),
        None,
    )


def _heading_occurrences(
    lines: Sequence[str],
    canonical_headings: Sequence[str],
    *,
    line_numbers: Sequence[int],
) -> dict[str, list[int]]:
    occurrences: dict[str, list[int]] = {}
    for line, line_number in zip(lines, line_numbers):
        canonical = _canonical_authority_heading(line, canonical_headings)
        if canonical is not None:
            occurrences.setdefault(canonical, []).append(line_number)
    return occurrences


def _noncanonical_heading_errors(
    lines: Sequence[str], separator: int
) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []

    for line_number, line in enumerate(lines[2:separator], start=3):
        canonical = _canonical_authority_heading(line, TOP_LEVEL_HEADINGS)
        if canonical is None or line == canonical:
            continue
        errors.append(
            {
                "rule": "section_heading_noncanonical",
                "heading": line,
                "canonical_heading": canonical,
                "line": line_number,
                "message": (
                    f"non-canonical section heading {line}; use {canonical}"
                ),
            }
        )

    if separator + 1 < len(lines):
        line = lines[separator + 1]
        canonical = _canonical_authority_heading(line, (PROCESS_HEADING,))
        if canonical is not None and line != canonical:
            errors.append(
                {
                    "rule": "process_heading_noncanonical",
                    "heading": line,
                    "canonical_heading": canonical,
                    "line": separator + 2,
                    "message": (
                        f"non-canonical process heading {line}; use {canonical}"
                    ),
                }
            )

    for line_number, line in enumerate(lines[separator + 2 :], start=separator + 3):
        canonical = _canonical_authority_heading(line, PROCESS_SUBHEADINGS)
        if canonical is None or line == canonical:
            continue
        errors.append(
            {
                "rule": "process_subheading_noncanonical",
                "heading": line,
                "canonical_heading": canonical,
                "line": line_number,
                "message": (
                    f"non-canonical process subheading {line}; use {canonical}"
                ),
            }
        )
    return errors


def _duplicate_field_errors(
    lines: Sequence[str],
    names: Sequence[str],
    *,
    context: str,
    line_numbers: Sequence[int],
) -> list[dict[str, object]]:
    occurrences: dict[str, list[int]] = {}
    for line, line_number in zip(lines, line_numbers):
        match, _compatibility_folded = _authority_field_match(line)
        if match:
            canonical = _canonical_authority_label(match.group(1), names)
            if canonical is not None:
                occurrences.setdefault(canonical, []).append(line_number)
    return [
        {
            "rule": f"{context}_field_duplicate",
            "field": label,
            "occurrences": len(positions),
            "lines": positions,
            "message": f"duplicate {context} field {label}",
        }
        for label in names
        if len(positions := occurrences.get(label, [])) > 1
    ]


def _reject_field_label_errors(
    lines: Sequence[str],
    names: Sequence[str],
    *,
    context: str,
    line_numbers: Sequence[int],
) -> None:
    duplicate_errors = _duplicate_field_errors(
        lines, names, context=context, line_numbers=line_numbers
    )
    if duplicate_errors:
        first = duplicate_errors[0]
        raise DocumentParseError(
            str(first["rule"]),
            str(first["message"]),
            details={
                "field": first["field"],
                "occurrences": first["occurrences"],
                "lines": first["lines"],
            },
            errors=duplicate_errors,
        )

    label_errors = _field_label_errors(
        lines, names, context=context, line_numbers=line_numbers
    )
    if label_errors:
        first = label_errors[0]
        raise DocumentParseError(
            str(first["rule"]),
            str(first["message"]),
            details={
                key: value
                for key, value in first.items()
                if key not in {"rule", "message"}
            },
            errors=label_errors,
        )


def preflight_planning_authority_labels(text: str) -> None:
    """Reject duplicate or non-canonical Planning labels without mutating workflow state."""
    lines = text.strip().splitlines()
    line_numbers = list(range(1, len(lines) + 1))
    if lines and lines[0] == "### Planning brief":
        lines = lines[1:]
        line_numbers = line_numbers[1:]
    _reject_field_label_errors(
        lines,
        PLANNING_FIELDS,
        context="planning",
        line_numbers=line_numbers,
    )


def _parse_exact_fields(
    lines: Sequence[str],
    names: Sequence[str],
    *,
    context: str,
    line_numbers: Sequence[int] | None = None,
) -> dict[str, str]:
    exact_line_numbers = list(line_numbers or range(1, len(lines) + 1))
    _reject_field_label_errors(
        lines, names, context=context, line_numbers=exact_line_numbers
    )

    values: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        match = re.match(r"^([^:]+):(?:\s*(.*))$", line)
        if match and match.group(1) in names:
            label, value = match.group(1), match.group(2)
            values[label] = value
            current = label
        elif current is not None and line.strip():
            values[current] = f"{values[current]}\n{line}"
        elif line.strip():
            raise DocumentParseError(f"{context}_field_unknown", f"unexpected line in {context}: {line}")
    missing = [name for name in names if name not in values]
    if missing:
        raise DocumentParseError(
            f"{context}_field_missing",
            f"missing fields: {', '.join(missing)}",
            details={
                "missing_fields": missing,
                "required_labels": [f"{name}: <value>" for name in missing],
            },
        )
    return values


def parse_planning_brief(text: str) -> PlanningBrief:
    lines = text.strip().splitlines()
    line_numbers = list(range(1, len(lines) + 1))
    if lines and lines[0] == "### Planning brief":
        lines = lines[1:]
        line_numbers = line_numbers[1:]
    return PlanningBrief(
        _parse_exact_fields(
            lines,
            PLANNING_FIELDS,
            context="planning",
            line_numbers=line_numbers,
        )
    )


def render_planning_brief_notes(brief: PlanningBrief) -> str:
    return brief.render(heading=True).rstrip() + "\n"


def parse_canonical_planning_notes(text: str) -> PlanningBrief:
    brief = parse_planning_brief(text)
    if text != render_planning_brief_notes(brief):
        raise DocumentParseError(
            "planning_brief_noncanonical",
            "Planning brief text is not in canonical rendered form",
            details={"required_heading": "### Planning brief"},
        )
    return brief


def document_shape(notes: str) -> str:
    """Classify live task notes by structural markers, without attempting a parse.

    Marker presence, not a catch-and-retry parse, decides which grammar governs:
    notes containing either process-record marker are asserting canonical shape,
    so a canonical parse failure on them is a genuine defect and must never fall
    through to being read as a Planning brief. Marker-absent notes can only
    legitimately be a Planning-stage brief or nothing at all.
    """
    if not notes.strip():
        return "bare"
    lines = notes.splitlines()
    if "---" in lines or "## PROCESS RECORD" in lines:
        return "canonical"
    return "planning_brief"


def _canonical_duplicate_errors(
    lines: Sequence[str], separator: int
) -> list[dict[str, object]]:
    section_positions = _heading_occurrences(
        lines[2:separator],
        TOP_LEVEL_HEADINGS,
        line_numbers=list(range(3, separator + 1)),
    )
    errors: list[dict[str, object]] = [
        {
            "rule": "section_duplicate",
            "heading": (
                canonical
                if canonical == "WHY COOK IT"
                else canonical[3:]
            ),
            "occurrences": len(positions),
            "lines": positions,
            "message": (
                "duplicate section "
                + (canonical if canonical == "WHY COOK IT" else canonical[3:])
            ),
        }
        for canonical in TOP_LEVEL_HEADINGS
        if len(positions := section_positions.get(canonical, [])) > 1
    ]

    process_start = separator + 2
    subheading_positions = _heading_occurrences(
        lines[process_start:],
        PROCESS_SUBHEADINGS,
        line_numbers=list(range(process_start + 1, len(lines) + 1)),
    )
    errors.extend(
        {
            "rule": "process_subheading_duplicate",
            "heading": heading,
            "occurrences": len(positions),
            "lines": positions,
            "message": f"duplicate process subheading {heading}",
        }
        for heading in PROCESS_SUBHEADINGS
        if len(positions := subheading_positions.get(heading, [])) > 1
    )
    try:
        planning_at = lines.index("### Planning brief", process_start)
    except ValueError:
        return errors
    errors.extend(
        _duplicate_field_errors(
            lines[process_start:planning_at],
            STATE_FIELDS,
            context="state",
            line_numbers=list(range(process_start + 1, planning_at + 1)),
        )
    )
    planning_end = len(lines)
    for heading in ("### Decisions", "### Research basis", "### Material changes"):
        try:
            candidate = lines.index(heading, planning_at + 1)
        except ValueError:
            continue
        planning_end = min(planning_end, candidate)
    errors.extend(
        _duplicate_field_errors(
            lines[planning_at + 1:planning_end],
            PLANNING_FIELDS,
            context="planning",
            line_numbers=list(range(planning_at + 2, planning_end + 1)),
        )
    )
    return errors


def _split_process(lines: Sequence[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    indexes = {line: i for i, line in enumerate(lines) if line in {"### Planning brief", "### Decisions", "### Research basis", "### Material changes"}}
    required = ("### Planning brief", "### Research basis")
    if any(name not in indexes for name in required):
        raise DocumentParseError("process_subheading_missing", "Planning brief and Research basis are required")
    ordered = sorted((index, name) for name, index in indexes.items())
    if [name for _, name in ordered] != [name for name in ("### Planning brief", "### Decisions", "### Research basis", "### Material changes") if name in indexes]:
        raise DocumentParseError("process_subheading_order", "process subheadings are not canonical")
    chunks: dict[str, list[str]] = {}
    for pos, (start, name) in enumerate(ordered):
        end = ordered[pos + 1][0] if pos + 1 < len(ordered) else len(lines)
        chunks[name] = list(lines[start + 1:end])
    return (
        chunks["### Planning brief"],
        chunks.get("### Decisions", []),
        chunks["### Research basis"],
        chunks.get("### Material changes", []),
    )


def parse_task_document(text: str) -> CanonicalTaskDocument:
    lines = text.rstrip("\n").splitlines()
    if len(lines) < 4:
        raise DocumentParseError("document_too_short", "canonical task is incomplete")
    if "---" not in lines:
        raise DocumentParseError("process_separator_missing", "missing process-record separator")
    separator = lines.index("---")

    duplicate_errors = _canonical_duplicate_errors(lines, separator)
    if duplicate_errors:
        first = duplicate_errors[0]
        raise DocumentParseError(
            str(first["rule"]),
            str(first["message"]),
            details={
                key: value
                for key, value in first.items()
                if key not in {"rule", "message"}
            },
            errors=duplicate_errors,
        )

    heading_errors = _noncanonical_heading_errors(lines, separator)
    if heading_errors:
        first = heading_errors[0]
        raise DocumentParseError(
            str(first["rule"]),
            str(first["message"]),
            details={
                key: value
                for key, value in first.items()
                if key not in {"rule", "message"}
            },
            errors=heading_errors,
        )

    if separator + 1 >= len(lines) or lines[separator + 1] != PROCESS_HEADING:
        raise DocumentParseError("process_heading_missing", "separator must be followed by PROCESS RECORD")

    title, recognition = lines[0], lines[1]
    body_lines = lines[2:separator]
    sections: dict[str, str] = {}
    introduction: list[str] = []
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if current is None:
            introduction.extend(buffer)
        else:
            if current in sections:
                raise DocumentParseError("section_duplicate", f"duplicate section {current}")
            sections[current] = "\n".join(buffer).strip()
        buffer = []

    for line in body_lines:
        if line == "WHY COOK IT":
            flush(); current = "WHY COOK IT"
        elif line.startswith("## "):
            flush(); current = line[3:]
            if current not in ALLOWED_SECTIONS:
                raise DocumentParseError("top_level_section_unknown", f"unexpected top-level section {current}")
        else:
            buffer.append(line)
    flush()

    process = lines[separator + 2:]
    planning_at = process.index("### Planning brief") if "### Planning brief" in process else -1
    if planning_at < 0:
        raise DocumentParseError("planning_brief_missing", "missing Planning brief")
    state = TaskState(_parse_exact_fields(process[:planning_at], STATE_FIELDS, context="state"))
    planning_lines, decisions, research_basis, material_changes = _split_process(process[planning_at:])
    if not research_basis:
        raise DocumentParseError("research_basis_missing", "Research basis is empty")

    schema_line = None
    for candidate in (material_changes, research_basis):
        if candidate and candidate[-1].startswith("Schema version:"):
            schema_line = candidate.pop()
            break
    if schema_line is None:
        raise DocumentParseError("schema_version_missing", "missing closing Schema version")
    schema_version = schema_line.split(":", 1)[1].strip()
    return CanonicalTaskDocument(
        title=title,
        recognition=recognition,
        introduction=tuple(introduction),
        sections=sections,
        state=state,
        planning_brief=PlanningBrief(_parse_exact_fields(planning_lines, PLANNING_FIELDS, context="planning")),
        decisions=tuple(decisions),
        research_basis=tuple(research_basis),
        material_changes=tuple(material_changes),
        schema_version=schema_version,
    )


def _none(value: str) -> bool:
    return value == "None"


def validate_planning_brief(brief: PlanningBrief) -> DocumentValidation:
    findings: list[DocumentFinding] = []
    role = brief.values["Role"]
    if role != "main" and not role.startswith("non-main — "):
        findings.append(DocumentFinding("planning.role", FindingKind.SYNTAX, "Role must be exactly `main` with no trailing text, or `non-main — <kind and why>`", "Role"))
    destination = brief.values["Destination section"]
    if destination not in {"[destination missing]", "[destination invalid]"} and not DESTINATION_RE.match(destination):
        findings.append(DocumentFinding("planning.destination", FindingKind.AGENT_CORRECTABLE, "Destination section must be name — gid or a canonical defect marker", "Destination section"))
    return DocumentValidation(tuple(findings))


def _material_change_findings(line: str, *, index: int) -> tuple[DocumentFinding, ...]:
    """Return all detectable grammar defects for one seven-field audit entry."""
    findings: list[DocumentFinding] = []
    location = f"Material changes[{index}]"
    parts = line.split(" — ", 6)
    if len(parts) != 7:
        return (
            DocumentFinding(
                "material-changes.format",
                FindingKind.SYNTAX,
                f"Material changes entries require exactly seven fields in this order: {MATERIAL_CHANGE_ACCEPTED_SYNTAX}",
                location,
            ),
            DocumentFinding(
                "material-changes.field-count",
                FindingKind.SYNTAX,
                f"expected seven fields separated by ' — '; found {len(parts)}",
                location,
            ),
        )

    date, agent, model, change, reason, materiality, verification = parts
    if re.fullmatch(DATE_PATTERN, date) is None:
        findings.append(DocumentFinding(
            "material-changes.date", FindingKind.SYNTAX,
            "date must use YYYY-MM-DD", f"{location}.date",
        ))
    if re.fullmatch(ACTOR_NAME_PATTERN, agent) is None:
        findings.append(DocumentFinding(
            "material-changes.agent", FindingKind.SYNTAX,
            "agent must be ChatGPT, Custom GPT, Claude, or Codex", f"{location}.agent",
        ))
    if not model.strip() or re.fullmatch(MODEL_PATTERN, model) is None:
        findings.append(DocumentFinding(
            "material-changes.model", FindingKind.SYNTAX,
            "model metadata is required and must not contain a comma or em dash", f"{location}.model",
        ))
    if not change.strip():
        findings.append(DocumentFinding(
            "material-changes.change", FindingKind.SYNTAX,
            "change must describe the concrete edit", f"{location}.change",
        ))
    if not reason.strip():
        findings.append(DocumentFinding(
            "material-changes.reason", FindingKind.SYNTAX,
            "reason is required", f"{location}.reason",
        ))
    if materiality not in {"Small", "Large"}:
        findings.append(DocumentFinding(
            "material-changes.materiality", FindingKind.SYNTAX,
            "materiality must be Small or Large", f"{location}.materiality",
        ))

    if verification != "pending-verification":
        verified = re.fullmatch(
            rf"verified — (?P<agent>{ACTOR_NAME_PATTERN}), (?P<model>{MODEL_PATTERN}), (?P<date>{DATE_PATTERN})",
            verification,
        )
        if verified is None:
            findings.append(DocumentFinding(
                "material-changes.verification", FindingKind.SYNTAX,
                "verification must be pending-verification or verified — <agent>, <model metadata>, <YYYY-MM-DD>",
                f"{location}.verification",
            ))

    if findings:
        findings.insert(0, DocumentFinding(
            "material-changes.format",
            FindingKind.SYNTAX,
            f"Material changes entry must use: {MATERIAL_CHANGE_ACCEPTED_SYNTAX}",
            location,
        ))
    return tuple(findings)


def validate_task_document(document: CanonicalTaskDocument, *, expected_schema_version: str | None = None, schema: Mapping[str, object] | None = None) -> DocumentValidation:
    findings: list[DocumentFinding] = list(validate_planning_brief(document.planning_brief).findings)
    task_schema = schema.get("task_document") if schema else None
    if schema and not isinstance(task_schema, Mapping):
        findings.append(DocumentFinding("schema.runtime-shape", FindingKind.SYNTAX, "runtime task schema is missing task_document", "schema"))
        task_schema = {}
    required_sections = tuple(name for name in task_schema.get("required_sections", REQUIRED_SECTIONS) if name != "PROCESS RECORD") if isinstance(task_schema, Mapping) else REQUIRED_SECTIONS
    allowed_statuses = frozenset(task_schema.get("allowed_statuses", ALLOWED_STATUSES)) if isinstance(task_schema, Mapping) else ALLOWED_STATUSES
    classifications = tuple(task_schema.get("research_basis_classifications", RESEARCH_BASIS_PREFIXES)) if isinstance(task_schema, Mapping) else RESEARCH_BASIS_PREFIXES
    human_prefix = str(task_schema.get("human_decision_prefix", "Human — Marco:")) if isinstance(task_schema, Mapping) else "Human — Marco:"
    destination_markers = tuple(task_schema.get("destination_defect_markers", ("destination missing", "destination invalid"))) if isinstance(task_schema, Mapping) else ("destination missing", "destination invalid")
    for section in required_sections:
        if not document.sections.get(section):
            findings.append(DocumentFinding("document.required-section", FindingKind.SYNTAX, f"missing required section {section}", section))
    if document.is_non_main:
        if not document.title.startswith("[non-main] "):
            findings.append(DocumentFinding("title.non-main-spacing", FindingKind.AGENT_CORRECTABLE, "[non-main] must be the leading role tag", "title"))
    elif re.match(r"^\[[^]]+\]", document.title) and document.title.startswith("["):
        first = document.title[1:].split("]", 1)[0]
        if first not in {"destination missing", "destination invalid"}:
            findings.append(DocumentFinding("title.role-tag", FindingKind.SYNTAX, "only [non-main] is a role tag", "title"))
    if " — " not in document.title:
        findings.append(DocumentFinding("title.recognition", FindingKind.SYNTAX, "title requires dish name — recognition phrase", "title"))
    destination = document.planning_brief.values["Destination section"]
    for marker in destination_markers:
        in_title = f"[{marker}]" in document.title
        in_field = destination == f"[{marker}]"
        if in_title != in_field:
            findings.append(DocumentFinding("title.destination-marker", FindingKind.AGENT_CORRECTABLE, "destination marker must agree between title and Destination section", "title"))

    status = document.state.values["Status"]
    if status not in allowed_statuses:
        findings.append(DocumentFinding("state.status", FindingKind.SYNTAX, f"unknown Status {status}", "Status"))
    else:
        detail, resume = document.state.values["Status detail"], document.state.values["Resume status"]
        release = document.state.values["Verification protocol release"]
        verified, self_verified = document.state.values["Verified by"], document.state.values["Self-verified"]
        illegal = False
        if status == "pending-research": illegal = _none(detail) or not (_none(resume) and _none(release) and _none(verified))
        elif status == "pending-evidence":
            illegal = _none(detail) or resume not in {"pending-research", "pending-verification"} or not _none(verified)
            if resume == "pending-research":
                illegal = illegal or not _none(release)
            elif resume == "pending-verification":
                illegal = illegal or _none(release) or _none(self_verified)
        elif status == "pending-human-review":
            illegal = _none(detail) or resume not in {"pending-research", "pending-verification"} or not _none(verified)
            if resume == "pending-research":
                illegal = illegal or not _none(release)
            elif resume == "pending-verification":
                illegal = illegal or _none(release) or _none(self_verified)
        elif status == "pending-verification": illegal = not (_none(detail) and _none(resume) and not _none(release) and _none(verified) and not _none(self_verified))
        elif status == "ready": illegal = not (_none(detail) and _none(resume) and not _none(release) and not _none(verified) and not _none(self_verified))
        if illegal:
            findings.append(DocumentFinding("state.illegal-combination", FindingKind.ILLEGAL_COMBINATION, f"state fields are illegal for {status}", "PROCESS RECORD"))
    for field_name in ("Researched by", "Verified by", "Self-verified"):
        value = document.state.values[field_name]
        if value != "None" and not ACTOR_RE.match(value):
            findings.append(DocumentFinding("state.actor-format", FindingKind.SYNTAX, f"invalid {field_name} format", field_name))
    if expected_schema_version is not None and document.schema_version != expected_schema_version:
        findings.append(DocumentFinding("schema.version-mismatch", FindingKind.SCHEMA_VERSION, f"task declares schema {document.schema_version}; expected {expected_schema_version}", "Schema version"))
    if not any("Classification:" in line and line.split("Classification:", 1)[1].strip().startswith(classifications) for line in document.research_basis):
        findings.append(DocumentFinding("research-basis.classification", FindingKind.SYNTAX, "Research basis requires an explicit approved classification", "Research basis"))
    for line in document.decisions:
        if not line.startswith(human_prefix + " "):
            findings.append(DocumentFinding("decisions.human-format", FindingKind.SYNTAX, "Decisions entries must use Human — Marco format", "Decisions"))
    for index, line in enumerate(document.material_changes, start=1):
        findings.extend(_material_change_findings(line, index=index))
    if document.planning_brief.values["Role"].startswith("non-main") != document.is_non_main:
        findings.append(DocumentFinding("role.title-brief-disagreement", FindingKind.ILLEGAL_COMBINATION, "title role and Planning brief Role disagree", "title"))
    return DocumentValidation(tuple(findings))
