"""Public API for deterministic canonical Dish task documents.

Implementation is decomposed by responsibility into private modules; callers
should continue to import task-document syntax, types, parsing, and validation
from this module.
"""

from __future__ import annotations

from ._task_document_parsing import (
    document_shape,
    parse_canonical_planning_notes,
    parse_planning_brief,
    parse_task_document,
    preflight_planning_authority_labels,
    render_planning_brief_notes,
)
from ._task_document_syntax import (
    ACTOR_NAME_PATTERN,
    ACTOR_RE,
    ALLOWED_EXEMPTION_TAGS,
    ALLOWED_SECTIONS,
    ALLOWED_STATUSES,
    DATE_PATTERN,
    DESTINATION_RE,
    NATIVE_DESTINATION_RE,
    EXEMPTION_TAG_AT_START_RE,
    MATERIAL_CHANGE_ACCEPTED_SYNTAX,
    MATERIAL_CHANGE_RE,
    MODEL_PATTERN,
    OPTIONAL_SECTIONS,
    PLANNING_FIELDS,
    PROCESS_HEADING,
    PROCESS_SUBHEADINGS,
    REQUIRED_SECTIONS,
    RESEARCH_BASIS_PREFIXES,
    SECTION_ORDER,
    STATE_FIELDS,
    TOP_LEVEL_HEADINGS,
)
from ._task_document_types import (
    RECOVERY_SPECS,
    CanonicalTaskDocument,
    DocumentFinding,
    DocumentParseError,
    DocumentValidation,
    FindingKind,
    PlanningBrief,
    RecoverySpec,
    TaskState,
    document_parse_error_payloads,
    finding_payload,
)
from ._task_document_validation import (
    validate_planning_brief,
    validate_task_document,
)

__all__ = (
    "ACTOR_NAME_PATTERN",
    "ACTOR_RE",
    "ALLOWED_EXEMPTION_TAGS",
    "ALLOWED_SECTIONS",
    "ALLOWED_STATUSES",
    "CanonicalTaskDocument",
    "DATE_PATTERN",
    "DESTINATION_RE",
    "NATIVE_DESTINATION_RE",
    "DocumentFinding",
    "DocumentParseError",
    "DocumentValidation",
    "EXEMPTION_TAG_AT_START_RE",
    "FindingKind",
    "MATERIAL_CHANGE_ACCEPTED_SYNTAX",
    "MATERIAL_CHANGE_RE",
    "MODEL_PATTERN",
    "OPTIONAL_SECTIONS",
    "PLANNING_FIELDS",
    "PROCESS_HEADING",
    "PROCESS_SUBHEADINGS",
    "PlanningBrief",
    "RECOVERY_SPECS",
    "REQUIRED_SECTIONS",
    "RESEARCH_BASIS_PREFIXES",
    "RecoverySpec",
    "SECTION_ORDER",
    "STATE_FIELDS",
    "TOP_LEVEL_HEADINGS",
    "TaskState",
    "document_parse_error_payloads",
    "document_shape",
    "finding_payload",
    "parse_canonical_planning_notes",
    "parse_planning_brief",
    "parse_task_document",
    "preflight_planning_authority_labels",
    "render_planning_brief_notes",
    "validate_planning_brief",
    "validate_task_document",
)
