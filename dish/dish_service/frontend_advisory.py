"""Non-authorizing factual Stage 4 workflow advisory."""
from __future__ import annotations

_ADVISORIES = {
    "prepare_required": ("workflow.prepare_required", "The current operation needs preparation before it can proceed."),
    "await_verification": ("workflow.await_verification", "The current operation is waiting for Verification."),
    "held_evidence": ("workflow.held_evidence", "The current operation is waiting for required evidence."),
    "held_human": ("workflow.held_human", "The current operation is waiting for human review."),
    "await_submission": ("workflow.await_submission", "The current operation is waiting for governed submission."),
    "ready_move_failed": ("workflow.destination_repair", "The destination movement requires repair before completion."),
    "recovery_rehearsal": ("workflow.recovery_rehearsal", "The current operation is in recovery rehearsal."),
}


def workflow_advisory(phase: str | None) -> dict[str, str | bool]:
    code, message = _ADVISORIES.get(
        phase,
        ("workflow.none", "No next step is currently available."),
    )
    return {
        "code": code,
        "message": message,
        "perspective": "workflow",
        "invokable_by_frontend": False,
    }
