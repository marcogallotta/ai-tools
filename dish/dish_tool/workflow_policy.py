"""Pure current-workflow policy over an authoritative snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class WorkflowSnapshot:
    operation_status: str
    operation_phase: str
    persisted_actions: tuple[str, ...]
    live_status: str | None
    live_section_gid: str | None
    verification_queue_gid: str | None
    cycle_reviewed: bool
    latest_cycle_outcome: str | None
    latest_cycle_route: str | None
    validation_rules: tuple[str, ...]
    pending_steps: tuple[str, ...] = ()


def legal_actions(snapshot: WorkflowSnapshot) -> list[str]:
    """Return the sole legal-action answer for current workflow state."""
    if snapshot.operation_status not in {"open", "uncertain"}:
        return []
    if snapshot.pending_steps:
        return []
    phase = snapshot.operation_phase
    # Preparation is the route that turns a bare Planning task or Planning brief
    # into governed content, so pre-candidate canonical findings cannot suppress it.
    if phase == "prepare_required":
        return list(snapshot.persisted_actions) if snapshot.operation_status == "open" else []
    if snapshot.validation_rules:
        return []
    if phase == "await_verification":
        if (
            snapshot.live_status != "pending-verification"
            or snapshot.live_section_gid != snapshot.verification_queue_gid
        ):
            return []
        return ["approve", "reject"] if snapshot.cycle_reviewed else ["verify"]
    if phase == "await_submission" and snapshot.live_status != "ready":
        return []
    if phase == "held_evidence" and snapshot.live_status != "pending-evidence":
        return []
    if phase == "held_human":
        if snapshot.live_status != "pending-human-review":
            return []
        if snapshot.latest_cycle_outcome == "two-pass-hold":
            return ["reopen"]
        if snapshot.latest_cycle_route == "human_review":
            return ["record-human-decision"]
        return []
    if phase == "prepare_required" and snapshot.operation_status != "open":
        return []
    return list(snapshot.persisted_actions)


def action_is_legal(snapshot: WorkflowSnapshot, action: str) -> bool:
    return action in legal_actions(snapshot)
