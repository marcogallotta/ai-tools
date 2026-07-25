"""Deterministic parser, renderer, and validator for canonical dish task bodies."""

from __future__ import annotations

import re
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
ALLOWED_STATUSES = frozenset(
    {"pending-research", "pending-evidence", "pending-human-review", "pending-verification", "ready"}
)
RESEARCH_BASIS_PREFIXES = (
    "Source-backed dish", "Halal port of ", "Intentional test dish, human-approved",
)
DESTINATION_RE = re.compile(r"^(?P<name>.+?)\s+—\s+(?P<gid>[0-9]+)$")
ACTOR_RE = re.compile(r"^(?:ChatGPT|Claude) — .+, \d{4}-\d{2}-\d{2}$")
MATERIAL_CHANGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+—\s+.+")


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
    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


def _parse_exact_fields(lines: Sequence[str], names: Sequence[str], *, context: str) -> dict[str, str]:
    values: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        match = re.match(r"^([^:]+):(?:\s*(.*))$", line)
        if match and match.group(1) in names:
            label, value = match.group(1), match.group(2)
            if label in values:
                raise DocumentParseError(f"{context}_field_duplicate", f"duplicate {label}")
            values[label] = value
            current = label
        elif current is not None and line.strip():
            values[current] = f"{values[current]}\n{line}"
        elif line.strip():
            raise DocumentParseError(f"{context}_field_unknown", f"unexpected line in {context}: {line}")
    missing = [name for name in names if name not in values]
    if missing:
        raise DocumentParseError(f"{context}_field_missing", f"missing fields: {', '.join(missing)}")
    return values


def parse_planning_brief(text: str) -> PlanningBrief:
    lines = text.strip().splitlines()
    if lines and lines[0] == "### Planning brief":
        lines = lines[1:]
    return PlanningBrief(_parse_exact_fields(lines, PLANNING_FIELDS, context="planning"))


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
    if separator + 1 >= len(lines) or lines[separator + 1] != "## PROCESS RECORD":
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
        findings.append(DocumentFinding("planning.role", FindingKind.SYNTAX, "Role must be main or non-main with kind and reason", "Role"))
    destination = brief.values["Destination section"]
    if destination not in {"[destination missing]", "[destination invalid]"} and not DESTINATION_RE.match(destination):
        findings.append(DocumentFinding("planning.destination", FindingKind.AGENT_CORRECTABLE, "Destination section must be name — gid or a canonical defect marker", "Destination section"))
    return DocumentValidation(tuple(findings))


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
        elif status == "pending-evidence": illegal = _none(detail) or resume not in {"pending-research", "pending-verification"} or not _none(verified)
        elif status == "pending-human-review": illegal = _none(detail) or resume not in {"pending-research", "pending-verification"}
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
    for line in document.material_changes:
        if not MATERIAL_CHANGE_RE.match(line):
            findings.append(DocumentFinding("material-changes.format", FindingKind.SYNTAX, "Material changes entry requires date and editor/model", "Material changes"))
    if document.planning_brief.values["Role"].startswith("non-main") != document.is_non_main:
        findings.append(DocumentFinding("role.title-brief-disagreement", FindingKind.ILLEGAL_COMBINATION, "title role and Planning brief Role disagree", "title"))
    return DocumentValidation(tuple(findings))
