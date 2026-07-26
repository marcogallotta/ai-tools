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
    unresolved_attempts: tuple[str, ...] = ()
    migration_reconciliation_required: bool = False
    identity_matches: bool = True
    placement_matches: bool = True
    required_cycle_exists: bool = True
    signoff_bound: bool = True
    held_baseline_matches: bool = True


def legal_actions(snapshot: WorkflowSnapshot) -> list[str]:
    """Return actions that are executable against the same authoritative snapshot."""
    if snapshot.operation_status != "open":
        return []
    if (
        snapshot.pending_steps
        or snapshot.unresolved_attempts
        or snapshot.migration_reconciliation_required
        or not snapshot.identity_matches
        or not snapshot.placement_matches
        or not snapshot.held_baseline_matches
    ):
        return []
    phase = snapshot.operation_phase
    if phase == "prepare_required":
        return list(snapshot.persisted_actions)
    if snapshot.validation_rules:
        return []
    if phase == "await_verification":
        if not snapshot.required_cycle_exists:
            return []
        if (
            snapshot.live_status != "pending-verification"
            or snapshot.live_section_gid != snapshot.verification_queue_gid
        ):
            return []
        return ["approve", "reject"] if snapshot.cycle_reviewed else ["verify"]
    if phase == "await_submission":
        if snapshot.live_status != "ready" or not snapshot.signoff_bound:
            return []
        return list(snapshot.persisted_actions)
    if phase == "held_evidence":
        if snapshot.live_status != "pending-evidence":
            return []
        return list(snapshot.persisted_actions)
    if phase == "held_human":
        if snapshot.live_status != "pending-human-review":
            return []
        if snapshot.latest_cycle_outcome == "two-pass-hold":
            return ["reopen"]
        if snapshot.latest_cycle_route == "human_review":
            return ["record-human-decision"]
        return []
    return list(snapshot.persisted_actions)


def action_is_legal(snapshot: WorkflowSnapshot, action: str) -> bool:
    return action in legal_actions(snapshot)
