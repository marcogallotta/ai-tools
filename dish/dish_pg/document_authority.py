"""Canonical Dish document authority helpers for the PostgreSQL target.

The PostgreSQL command path stores only complete, parser-validated canonical
Dish documents.  Workflow state and submission destination are derived from
the parsed document rather than from ad-hoc text scanning or caller hints.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from dish_tool.errors import DishRuleError
from dish_tool.lifecycle import hold, ready, resumed
from dish_tool.governed_diff import preserve_material_change_history
from dish_tool.models import material_editor_line, verification_actor_line
from dish_tool.task_document import (
    DESTINATION_RE,
    CanonicalTaskDocument,
    DocumentParseError,
    TaskState,
    document_parse_error_payloads,
    finding_payload,
    parse_task_document,
    validate_task_document,
)


class CanonicalDocumentError(ValueError):
    """A candidate cannot become canonical PostgreSQL authority."""

    def __init__(self, message: str, *, errors: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.errors = tuple(errors or ())


@dataclass(frozen=True)
class CanonicalDocumentParts:
    document: CanonicalTaskDocument
    title: str
    body: str


def parse_canonical_document(
    *,
    title: str | None = None,
    body: str | None = None,
    file_text: str | None = None,
    expected_status: str | None = None,
    expected_schema_version: str | None = None,
) -> CanonicalDocumentParts:
    """Parse, validate, and canonically render one complete task document."""

    if file_text is not None:
        raw = str(file_text)
    elif title is not None and body is not None:
        raw = f"{title}\n{body}"
    else:
        raise CanonicalDocumentError("a complete canonical document is required")

    try:
        document = parse_task_document(raw)
    except DocumentParseError as exc:
        raise CanonicalDocumentError(
            "candidate is not a canonical Dish document",
            errors=document_parse_error_payloads(exc),
        ) from exc

    validation = validate_task_document(
        document,
        expected_schema_version=expected_schema_version,
    )
    errors = [finding_payload(item) for item in validation.findings]
    if expected_status is not None and document.state.values["Status"] != expected_status:
        errors.append(
            {
                "rule": "state.status-required",
                "message": f"candidate Status must be {expected_status}",
                "field": "Status",
                "current": document.state.values["Status"],
            }
        )
    rendered = document.render()
    # Parsing intentionally accepts a trailing-newline variation.  All other
    # differences are noncanonical authority input and must be rejected rather
    # than silently rewritten under the caller.
    if raw.rstrip("\n") + "\n" != rendered:
        errors.append(
            {
                "rule": "document.noncanonical-rendering",
                "message": "candidate text does not exactly match canonical rendering",
            }
        )
    if errors:
        raise CanonicalDocumentError("candidate failed deterministic validation", errors=errors)

    lines = rendered.splitlines()
    return CanonicalDocumentParts(
        document=document,
        title=lines[0],
        body="\n".join(lines[1:]) + "\n",
    )


def render_parts(document: CanonicalTaskDocument) -> CanonicalDocumentParts:
    rendered = document.render()
    lines = rendered.splitlines()
    return CanonicalDocumentParts(document, lines[0], "\n".join(lines[1:]) + "\n")


def prepared_document(
    file_text: str,
    *,
    agent: str,
    model: str,
    at: datetime,
) -> CanonicalDocumentParts:
    """Parse a fresh initial-operation Research candidate and stamp provenance.

    Mirrors ``dish_tool.step6.prepare_live``'s initial-operation handling:
    prepare deterministically owns and rewrites "Researched by"/"Self-verified"
    from the calling agent/model/date, regardless of what the candidate wrote
    there, rather than validating the caller's self-reported text as-is.
    """
    try:
        document = parse_task_document(str(file_text))
    except DocumentParseError as exc:
        raise CanonicalDocumentError(
            "candidate is not a canonical Dish document",
            errors=document_parse_error_payloads(exc),
        ) from exc
    try:
        actor_line = material_editor_line(agent, model, at.date().isoformat())
    except DishRuleError as exc:
        raise CanonicalDocumentError(str(exc)) from exc
    state = dict(document.state.values)
    state["Researched by"] = actor_line
    state["Self-verified"] = actor_line
    stamped = dataclasses.replace(document, state=TaskState(state))
    return _validated_parts(stamped, expected_status="pending-verification")


def ready_document(
    document: CanonicalTaskDocument,
    *,
    agent: str,
    model: str,
    at: datetime,
) -> CanonicalDocumentParts:
    try:
        verified_by = verification_actor_line(agent, model, at.date().isoformat())
        signed = dataclasses.replace(
            document,
            state=ready(document.state.values, verified_by=verified_by),
        )
    except DishRuleError as exc:
        raise CanonicalDocumentError(str(exc)) from exc
    if signed.material_changes:
        verified_state = f"verified — {verified_by.replace(' — ', ', ', 1)}"
        signed = dataclasses.replace(
            signed,
            material_changes=tuple(
                line.removesuffix("pending-verification") + verified_state
                if line.endswith(" — pending-verification")
                else line
                for line in signed.material_changes
            ),
        )
    return _validated_parts(signed, expected_status="ready")


def held_document(
    document: CanonicalTaskDocument,
    *,
    target: str,
    detail: str,
) -> CanonicalDocumentParts:
    try:
        held = dataclasses.replace(
            document,
            state=hold(
                document.state.values,
                target=target,
                detail=detail,
                resume_status="pending-verification",
            ),
        )
    except DishRuleError as exc:
        raise CanonicalDocumentError(str(exc)) from exc
    return _validated_parts(held, expected_status=target)


def resumed_document(
    document: CanonicalTaskDocument,
    *,
    decision_line: str,
    resume_status: str | None = None,
    candidate: CanonicalTaskDocument | None = None,
    editor: str | None = None,
    model: str | None = None,
    at: datetime | None = None,
) -> CanonicalDocumentParts:
    clean_decision = " ".join(str(decision_line).split())
    if not clean_decision.startswith("Human — Marco:"):
        raise CanonicalDocumentError(
            "hold resumption requires an exact Marco decision record"
        )
    if resume_status is not None and resume_status not in {
        "pending-research",
        "pending-verification",
    }:
        raise CanonicalDocumentError(
            "hold resumption status must be pending-research or pending-verification"
        )

    source = document
    if candidate is not None:
        if editor not in {"claude", "gpt", "codex"} or not str(model or "").strip() or at is None:
            raise CanonicalDocumentError(
                "material hold resumption requires editor, model, and timestamp"
            )
        candidate = preserve_material_change_history(document, candidate)
        values = dict(candidate.state.values)
        values["Researched by"] = document.state.values["Researched by"]
        values.update(
            {
                "Status": document.state.values["Status"],
                "Status detail": document.state.values["Status detail"],
                "Resume status": resume_status
                or document.state.values["Resume status"],
                "Verified by": "None",
                "Self-verified": material_editor_line(
                    editor, str(model).strip(), at.date().isoformat()
                ),
            }
        )
        if values["Resume status"] == "pending-verification":
            values["Verification protocol release"] = document.state.values[
                "Verification protocol release"
            ]
        source = dataclasses.replace(candidate, state=TaskState(values))
    elif resume_status is not None:
        values = dict(document.state.values)
        values["Resume status"] = resume_status
        source = dataclasses.replace(document, state=TaskState(values))

    decisions = tuple(source.decisions)
    if clean_decision in decisions:
        raise CanonicalDocumentError(
            "hold resumption decision must identify the exact hold occurrence"
        )
    try:
        resumed_value = dataclasses.replace(
            source,
            state=resumed(source.state.values),
            decisions=decisions + (clean_decision,),
        )
    except DishRuleError as exc:
        raise CanonicalDocumentError(str(exc)) from exc
    return _validated_parts(
        resumed_value,
        expected_status=resumed_value.state.values["Status"],
    )


def destination_gid(document: CanonicalTaskDocument) -> str:
    value = document.planning_brief.values["Destination section"]
    match = DESTINATION_RE.fullmatch(value)
    if match is None:
        raise CanonicalDocumentError(
            "signed document has no exact governed destination",
            errors=[
                {
                    "rule": "planning.destination",
                    "message": "Destination section must be name — gid",
                    "field": "Destination section",
                    "current": value,
                }
            ],
        )
    return match.group("gid")


def _validated_parts(
    document: CanonicalTaskDocument,
    *,
    expected_status: str,
) -> CanonicalDocumentParts:
    validation = validate_task_document(document)
    errors = [finding_payload(item) for item in validation.findings]
    if document.state.values["Status"] != expected_status:
        errors.append(
            {
                "rule": "state.status-transition",
                "message": f"rendered status is not {expected_status}",
            }
        )
    if errors:
        raise CanonicalDocumentError("generated canonical state failed validation", errors=errors)
    return render_parts(document)
