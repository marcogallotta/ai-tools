"""Parsing and structural diagnostics for canonical Dish task documents."""

from __future__ import annotations

from typing import Sequence

from ._task_document_syntax import (
    ALLOWED_SECTIONS,
    PLANNING_FIELDS,
    PROCESS_HEADING,
    PROCESS_SUBHEADINGS,
    STATE_FIELDS,
    TOP_LEVEL_HEADINGS,
    _authority_field_match,
    _canonical_authority_heading,
    _duplicate_field_errors,
    _field_label_errors,
    _heading_occurrences,
)
from ._task_document_types import (
    CanonicalTaskDocument,
    DocumentParseError,
    PlanningBrief,
    TaskState,
)


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
    """Reject non-canonical Planning authority syntax before workflow mutation."""
    lines = text.strip().splitlines()
    line_numbers = list(range(1, len(lines) + 1))
    heading_errors = [
        {
            "rule": "process_subheading_noncanonical",
            "heading": line,
            "canonical_heading": canonical,
            "line": line_number,
            "message": (
                f"non-canonical process subheading {line}; use {canonical}"
            ),
        }
        for line, line_number in zip(lines, line_numbers)
        if (
            (canonical := _canonical_authority_heading(line, PROCESS_SUBHEADINGS))
            is not None
            and line != canonical
        )
    ]
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
    for line, line_number in zip(lines, exact_line_numbers):
        match, _compatibility_folded = _authority_field_match(line)
        if match and match.group(1) in names:
            label, value = match.group(1), match.group(2)
            values[label] = value
            current = label
        elif match:
            label = match.group(1)
            raise DocumentParseError(
                f"{context}_field_unknown",
                f"unsupported {context} field: {label}",
                details={"field": label, "line": line_number},
            )
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
    preflight_planning_authority_labels(text)
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
    schema_positions = [
        line_number
        for line_number, line in enumerate(
            lines[process_start:], start=process_start + 1
        )
        if line.startswith("Schema version:")
    ]
    if len(schema_positions) > 1:
        errors.append(
            {
                "rule": "schema_version_duplicate",
                "occurrences": len(schema_positions),
                "lines": schema_positions,
                "message": "duplicate closing Schema version",
            }
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
