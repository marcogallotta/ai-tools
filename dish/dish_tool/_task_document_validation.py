"""Planning-brief and canonical task-document validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from ._task_document_material_changes import material_change_findings
from ._task_document_syntax import (
    ACTOR_RE,
    ALLOWED_EXEMPTION_TAGS,
    ALLOWED_STATUSES,
    DESTINATION_RE,
    EXEMPTION_TAG_AT_START_RE,
    PLANNING_FIELDS,
    REQUIRED_SECTIONS,
    RESEARCH_BASIS_PREFIXES,
    STATE_FIELDS,
)
from ._task_document_types import (
    CanonicalTaskDocument,
    DocumentFinding,
    DocumentValidation,
    FindingKind,
    PlanningBrief,
)


@dataclass(frozen=True)
class _DocumentValidationRules:
    required_sections: tuple[str, ...]
    allowed_statuses: frozenset[str]
    classifications: tuple[str, ...]
    human_prefix: str
    destination_markers: tuple[str, ...]


def validate_planning_brief(brief: PlanningBrief) -> DocumentValidation:
    findings: list[DocumentFinding] = []
    for field_name in PLANNING_FIELDS:
        if not brief.values[field_name].strip():
            findings.append(
                DocumentFinding(
                    "planning.field-empty",
                    FindingKind.SYNTAX,
                    f"{field_name} requires a non-empty value",
                    field_name,
                )
            )
    role = brief.values["Role"]
    if role and role != "main" and not role.startswith("non-main — "):
        findings.append(DocumentFinding(
            "planning.role",
            FindingKind.SYNTAX,
            "Role must be exactly `main` with no trailing text, or `non-main — <kind and why>`",
            "Role",
            current=role,
        ))
    exemptions = brief.values["Exemptions"]
    remainder = exemptions
    exemption_tags: list[str] = []
    while match := EXEMPTION_TAG_AT_START_RE.match(remainder):
        exemption_tags.append(match.group(1).strip())
        remainder = remainder[match.end():]
    unsupported_tags = sorted(set(exemption_tags) - ALLOWED_EXEMPTION_TAGS)
    if unsupported_tags:
        findings.append(
            DocumentFinding(
                "planning.exemption-tag-unsupported",
                FindingKind.SYNTAX,
                (
                    "Unsupported exemption tags: "
                    + ", ".join(f"[{tag}]" for tag in unsupported_tags)
                    + "; allowed tags are [nutrition-kcal], [nutrition-protein], "
                    "[nutrition-fat]"
                ),
                "Exemptions",
                current=", ".join(f"[{tag}]" for tag in unsupported_tags),
            )
        )
    destination = brief.values["Destination section"]
    if destination and destination not in {"[destination missing]", "[destination invalid]"} and not DESTINATION_RE.match(destination):
        findings.append(DocumentFinding(
            "planning.destination",
            FindingKind.AGENT_CORRECTABLE,
            "Destination section must be name — gid or a canonical defect marker",
            "Destination section",
            current=destination,
        ))
    return DocumentValidation(tuple(findings))


def _document_validation_rules(
    schema: Mapping[str, object] | None,
) -> tuple[_DocumentValidationRules, tuple[DocumentFinding, ...]]:
    task_schema = schema.get("task_document") if schema else None
    findings: list[DocumentFinding] = []
    if schema and not isinstance(task_schema, Mapping):
        findings.append(DocumentFinding(
            "schema.runtime-shape",
            FindingKind.SYNTAX,
            "runtime task schema is missing task_document",
            "schema",
        ))
        task_schema = {}

    if isinstance(task_schema, Mapping):
        rules = _DocumentValidationRules(
            required_sections=tuple(
                name
                for name in task_schema.get("required_sections", REQUIRED_SECTIONS)
                if name != "PROCESS RECORD"
            ),
            allowed_statuses=frozenset(
                task_schema.get("allowed_statuses", ALLOWED_STATUSES)
            ),
            classifications=tuple(
                task_schema.get(
                    "research_basis_classifications", RESEARCH_BASIS_PREFIXES
                )
            ),
            human_prefix=str(
                task_schema.get("human_decision_prefix", "Human — Marco:")
            ),
            destination_markers=tuple(
                task_schema.get(
                    "destination_defect_markers",
                    ("destination missing", "destination invalid"),
                )
            ),
        )
    else:
        rules = _DocumentValidationRules(
            required_sections=REQUIRED_SECTIONS,
            allowed_statuses=ALLOWED_STATUSES,
            classifications=RESEARCH_BASIS_PREFIXES,
            human_prefix="Human — Marco:",
            destination_markers=("destination missing", "destination invalid"),
        )
    return rules, tuple(findings)


def _document_content_findings(
    document: CanonicalTaskDocument,
    *,
    required_sections: tuple[str, ...],
) -> tuple[DocumentFinding, ...]:
    findings: list[DocumentFinding] = []
    for section in required_sections:
        if not document.sections.get(section):
            findings.append(DocumentFinding(
                "document.required-section",
                FindingKind.SYNTAX,
                f"missing required section {section}",
                section,
            ))

    quantities = document.sections.get("QUANTITIES")
    if quantities and not any(
        re.fullmatch(r"Portions:\s*\S.*", line)
        for line in quantities.splitlines()
    ):
        current_portions = next(
            (line for line in quantities.splitlines() if line.startswith("Portions:")),
            None,
        )
        findings.append(DocumentFinding(
            "quantities.portions-required",
            FindingKind.SYNTAX,
            "QUANTITIES requires a non-empty Portions: line",
            "QUANTITIES",
            current=current_portions,
        ))

    if not document.recognition.strip():
        findings.append(DocumentFinding(
            "document.recognition-empty",
            FindingKind.SYNTAX,
            "canonical line 2 requires a non-empty dish-summary/meal-role sentence",
            {"section": "canonical-header", "line": 2, "after": "title"},
            current={"line_1": document.title, "line_2": document.recognition},
        ))
    return tuple(findings)


def _title_findings(
    document: CanonicalTaskDocument,
    *,
    destination_markers: tuple[str, ...],
) -> tuple[DocumentFinding, ...]:
    findings: list[DocumentFinding] = []
    if document.is_non_main:
        if not document.title.startswith("[non-main] "):
            findings.append(DocumentFinding(
                "title.non-main-spacing",
                FindingKind.AGENT_CORRECTABLE,
                "[non-main] must be the leading role tag",
                "title",
                current=document.title,
            ))
    elif re.match(r"^\[[^]]+\]", document.title) and document.title.startswith("["):
        first = document.title[1:].split("]", 1)[0]
        if first not in {"destination missing", "destination invalid"}:
            findings.append(DocumentFinding(
                "title.role-tag",
                FindingKind.SYNTAX,
                "only [non-main] is a role tag",
                "title",
                current=document.title,
            ))

    if " — " not in document.title:
        findings.append(DocumentFinding(
            "title.recognition",
            FindingKind.SYNTAX,
            "title requires dish name — recognition phrase",
            "title",
            current=document.title,
        ))

    destination = document.planning_brief.values["Destination section"]
    for marker in destination_markers:
        in_title = f"[{marker}]" in document.title
        in_field = destination == f"[{marker}]"
        if in_title != in_field:
            findings.append(DocumentFinding(
                "title.destination-marker",
                FindingKind.AGENT_CORRECTABLE,
                "destination marker must agree between title and Destination section",
                "title",
                current=document.title,
                related={"title": document.title, "Destination section": destination},
            ))
    return tuple(findings)


def _none(value: str) -> bool:
    return value == "None"


def _state_combination_illegal(document: CanonicalTaskDocument, status: str) -> bool:
    detail = document.state.values["Status detail"]
    resume = document.state.values["Resume status"]
    release = document.state.values["Verification protocol release"]
    verified = document.state.values["Verified by"]
    self_verified = document.state.values["Self-verified"]

    if status == "pending-research":
        return _none(detail) or not (
            _none(resume) and _none(release) and _none(verified)
        )
    if status in {"pending-evidence", "pending-human-review"}:
        illegal = (
            _none(detail)
            or resume not in {"pending-research", "pending-verification"}
            or not _none(verified)
        )
        if resume == "pending-research":
            return illegal or not _none(release)
        if resume == "pending-verification":
            return illegal or _none(release) or _none(self_verified)
        return illegal
    if status == "pending-verification":
        return not (
            _none(detail)
            and _none(resume)
            and not _none(release)
            and _none(verified)
            and not _none(self_verified)
        )
    if status == "ready":
        return not (
            _none(detail)
            and _none(resume)
            and not _none(release)
            and not _none(verified)
            and not _none(self_verified)
        )
    return False


def _state_findings(
    document: CanonicalTaskDocument,
    *,
    allowed_statuses: frozenset[str],
) -> tuple[DocumentFinding, ...]:
    findings: list[DocumentFinding] = []
    status = document.state.values["Status"]
    if status not in allowed_statuses:
        findings.append(DocumentFinding(
            "state.status",
            FindingKind.SYNTAX,
            f"unknown Status {status}",
            "Status",
            current=status,
        ))
    elif _state_combination_illegal(document, status):
        findings.append(DocumentFinding(
            "state.illegal-combination",
            FindingKind.ILLEGAL_COMBINATION,
            f"state fields are illegal for {status}",
            "PROCESS RECORD",
            related={name: document.state.values[name] for name in STATE_FIELDS},
        ))

    for field_name in ("Researched by", "Verified by", "Self-verified"):
        value = document.state.values[field_name]
        if value != "None" and not ACTOR_RE.match(value):
            findings.append(DocumentFinding(
                "state.actor-format",
                FindingKind.SYNTAX,
                f"invalid {field_name} format",
                field_name,
                current=value,
            ))
    return tuple(findings)


def _schema_version_findings(
    document: CanonicalTaskDocument,
    *,
    expected_schema_version: str | None,
) -> tuple[DocumentFinding, ...]:
    if expected_schema_version is None or document.schema_version == expected_schema_version:
        return ()
    return (
        DocumentFinding(
            "schema.version-mismatch",
            FindingKind.SCHEMA_VERSION,
            f"task declares schema {document.schema_version}; expected {expected_schema_version}",
            "Schema version",
            current=document.schema_version,
        ),
    )


def _research_basis_findings(
    document: CanonicalTaskDocument,
    *,
    classifications: tuple[str, ...],
) -> tuple[DocumentFinding, ...]:
    classification_lines = tuple(
        line for line in document.research_basis if "Classification:" in line
    )
    if any(
        line.split("Classification:", 1)[1].strip().startswith(classifications)
        for line in classification_lines
    ):
        return ()
    return (
        DocumentFinding(
            "research-basis.classification",
            FindingKind.SYNTAX,
            "Research basis requires an explicit approved classification",
            "Research basis",
            current=classification_lines[0] if len(classification_lines) == 1 else None,
        ),
    )


def _decision_findings(
    document: CanonicalTaskDocument,
    *,
    human_prefix: str,
) -> tuple[DocumentFinding, ...]:
    return tuple(
        DocumentFinding(
            "decisions.human-format",
            FindingKind.SYNTAX,
            "Decisions entries must use Human — Marco format",
            "Decisions",
            current=line,
        )
        for line in document.decisions
        if not line.startswith(human_prefix + " ")
    )


def _material_change_findings(
    document: CanonicalTaskDocument,
) -> tuple[DocumentFinding, ...]:
    findings: list[DocumentFinding] = []
    for index, line in enumerate(document.material_changes, start=1):
        findings.extend(material_change_findings(line, index=index))
    return tuple(findings)


def _role_findings(document: CanonicalTaskDocument) -> tuple[DocumentFinding, ...]:
    role = document.planning_brief.values["Role"]
    if role.startswith("non-main") == document.is_non_main:
        return ()
    return (
        DocumentFinding(
            "role.title-brief-disagreement",
            FindingKind.ILLEGAL_COMBINATION,
            "title role and Planning brief Role disagree",
            "title",
            current=document.title,
            related={"title": document.title, "Role": role},
        ),
    )


def validate_task_document(
    document: CanonicalTaskDocument,
    *,
    expected_schema_version: str | None = None,
    schema: Mapping[str, object] | None = None,
) -> DocumentValidation:
    """Validate one parsed canonical document in deterministic finding order."""
    findings: list[DocumentFinding] = list(
        validate_planning_brief(document.planning_brief).findings
    )
    rules, schema_findings = _document_validation_rules(schema)
    findings.extend(schema_findings)
    findings.extend(
        _document_content_findings(
            document,
            required_sections=rules.required_sections,
        )
    )
    findings.extend(
        _title_findings(document, destination_markers=rules.destination_markers)
    )
    findings.extend(
        _state_findings(document, allowed_statuses=rules.allowed_statuses)
    )
    findings.extend(
        _schema_version_findings(
            document,
            expected_schema_version=expected_schema_version,
        )
    )
    findings.extend(
        _research_basis_findings(document, classifications=rules.classifications)
    )
    findings.extend(_decision_findings(document, human_prefix=rules.human_prefix))
    findings.extend(_material_change_findings(document))
    findings.extend(_role_findings(document))
    return DocumentValidation(tuple(findings))
