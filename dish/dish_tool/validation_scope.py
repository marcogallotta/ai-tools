"""Stable machine-readable validation-scope reporting."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


STRUCTURAL_ONLY = "structural-only"
TRANSITION_STATE = "transition-state"
EXACT_CONTENT_IDENTITY = "exact-content-identity"
AGENT_SEMANTIC_REVIEW = "agent-semantic-review"
PROVENANCE_SIGNOFF = "provenance-signoff"
MOVEMENT_CONFIRMATION = "movement-confirmation"

VALIDATION_SCOPE_VALUES = (
    STRUCTURAL_ONLY,
    TRANSITION_STATE,
    EXACT_CONTENT_IDENTITY,
    AGENT_SEMANTIC_REVIEW,
    PROVENANCE_SIGNOFF,
    MOVEMENT_CONFIRMATION,
)

PLANNING_PREPARE_SCOPE = (
    STRUCTURAL_ONLY,
    TRANSITION_STATE,
    EXACT_CONTENT_IDENTITY,
)
RESEARCH_PREPARE_SCOPE = (
    STRUCTURAL_ONLY,
    TRANSITION_STATE,
    EXACT_CONTENT_IDENTITY,
)
VERIFICATION_APPROVE_SCOPE = (
    STRUCTURAL_ONLY,
    TRANSITION_STATE,
    EXACT_CONTENT_IDENTITY,
    AGENT_SEMANTIC_REVIEW,
    PROVENANCE_SIGNOFF,
)
VERIFICATION_REJECT_SCOPE = (
    STRUCTURAL_ONLY,
    TRANSITION_STATE,
    EXACT_CONTENT_IDENTITY,
    AGENT_SEMANTIC_REVIEW,
)
SUBMIT_SCOPE = (
    STRUCTURAL_ONLY,
    TRANSITION_STATE,
    EXACT_CONTENT_IDENTITY,
    MOVEMENT_CONFIRMATION,
)


def scope_for_command(
    command: str, *, operation_kind: str | None = None
) -> tuple[str, ...]:
    if command == "prepare":
        if operation_kind == "planning":
            return PLANNING_PREPARE_SCOPE
        if operation_kind in {"initial", "change"}:
            return RESEARCH_PREPARE_SCOPE
        return ()
    if command == "approve":
        return VERIFICATION_APPROVE_SCOPE
    if command == "reject":
        return VERIFICATION_REJECT_SCOPE
    if command == "submit":
        return SUBMIT_SCOPE
    return ()


def add_validation_scope(
    data: dict[str, Any], scope: Sequence[str] | None
) -> dict[str, Any]:
    if scope:
        data["validation_scope"] = list(scope)
    return data
