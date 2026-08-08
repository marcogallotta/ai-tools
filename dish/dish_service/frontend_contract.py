"""Closed presentation registries for the read-only browser frontend.

These registries translate durable PostgreSQL facts into browser labels. They do
not decide workflow legality and must never be used as mutation authority.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

FRONTEND_CONTRACT_VERSION = "dish-frontend-v1"
BOARD_QUERY_CONTRACT_VERSION = "frontend-board-query-v1"
NORMALIZATION_CONTRACT_VERSION = "frontend-normalization-v1-candidate"


@dataclass(frozen=True, slots=True)
class AttentionPresentation:
    code: str
    label: str
    severity: str


ATTENTION_PRESENTATIONS: tuple[AttentionPresentation, ...] = (
    AttentionPresentation("isolated", "ISOLATED", "warning"),
    AttentionPresentation("lease_attention", "Lease needs attention", "warning"),
    AttentionPresentation(
        "verification_attention", "Verification needs attention", "warning"
    ),
    AttentionPresentation("hold_active", "On hold", "warning"),
    AttentionPresentation("recovery_required", "Recovery required", "error"),
    AttentionPresentation("abandonment_active", "Abandonment active", "error"),
    AttentionPresentation("succession_active", "Succession active", "error"),
    AttentionPresentation("projection_abnormal", "Asana projection issue", "warning"),
)
ATTENTION_BY_CODE = {item.code: item for item in ATTENTION_PRESENTATIONS}

_OPERATION_LABELS = {
    "planning": "Planning",
    "initial": "Initial",
    "change": "Change",
    "verification": "Verification",
    "migration": "Migration",
}
_PHASE_LABELS = {
    "prepare_required": "Prepare required",
    "await_verification": "Await verification",
    "held_evidence": "Evidence hold",
    "held_human": "Human review",
    "await_submission": "Await submission",
    "ready_move_failed": "Destination repair required",
    "recovery_rehearsal": "Recovery rehearsal",
}
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_label(value: str) -> str:
    """Candidate Stage 3 label normalization.

    This is intentionally versioned because Gate B still needs to reconcile the
    exact Python/SQL comparison contract before HTTP activation.
    """

    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold()


def operation_status(kind: str | None, phase: str | None) -> dict[str, str]:
    """Return the closed factual workflow-status DTO or fail on unknown state."""

    if kind is None and phase is None:
        return {"state": "no_active_operation"}
    if kind is None or phase is None:
        raise ValueError("open operation kind and phase must be present together")
    try:
        operation_label = _OPERATION_LABELS[kind]
        phase_label = _PHASE_LABELS[phase]
    except KeyError as exc:
        raise ValueError(f"unregistered open workflow presentation value: {exc.args[0]}") from None
    return {
        "state": "active_operation",
        "operation": operation_label,
        "phase": phase_label,
    }
