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
    verifier_established: bool
    latest_cycle_outcome: str | None
    latest_cycle_route: str | None
    validation_rules: tuple[str, ...]
    operation_kind: str = ""
    pending_steps: tuple[str, ...] = ()
    unresolved_attempts: tuple[str, ...] = ()
    migration_reconciliation_required: bool = False
    identity_matches: bool = True
    placement_matches: bool = True
    required_cycle_exists: bool = True
    signoff_bound: bool = True
    held_baseline_matches: bool = True
    preconstruction_hold: bool = False
    destination_repair_required: bool = False
    dish_inspect_current: bool = False
    semantic_proposal_status: str | None = None
    semantic_proposal_actionable: bool = False
    abandonment_status: str | None = None
    abandonment_required_command: str | None = None
    abandonment_required_start_kind: str | None = None
    abandonment_continuation_ready: bool = False


def legal_actions(snapshot: WorkflowSnapshot) -> list[str]:
    """Return actions that are executable against the same authoritative snapshot."""
    if snapshot.operation_status != "open":
        return []
    if (
        snapshot.pending_steps
        or snapshot.unresolved_attempts
        or snapshot.migration_reconciliation_required
    ):
        return []
    if snapshot.semantic_proposal_status in {"pending", "approved", "claimed"}:
        if snapshot.abandonment_status is not None:
            return []
        if (
            snapshot.semantic_proposal_status in {"approved", "claimed"}
            and snapshot.semantic_proposal_actionable
        ):
            return ["apply-proposal"]
        return []
    if snapshot.abandonment_status is not None:
        if snapshot.abandonment_status == "completed":
            if (
                snapshot.abandonment_continuation_ready
                and snapshot.abandonment_required_command == "start"
            ):
                return [
                    "verify"
                    if snapshot.abandonment_required_start_kind == "verification"
                    else "start"
                ]
            return []
        if (
            snapshot.abandonment_status == "awaiting_successor_claim"
            and snapshot.abandonment_required_command == "start"
        ):
            return [
                "verify"
                if snapshot.abandonment_required_start_kind == "verification"
                else "start"
            ]
        return []
    if (
        not snapshot.identity_matches
        or not snapshot.placement_matches
        or not snapshot.held_baseline_matches
    ):
        return []
    phase = snapshot.operation_phase
    if phase == "prepare_required":
        return [
            action for action in snapshot.persisted_actions
            if action != "reject" or snapshot.operation_kind == "initial"
        ]
    if phase == "held_evidence" and snapshot.preconstruction_hold:
        return list(snapshot.persisted_actions)
    if phase == "held_human" and snapshot.preconstruction_hold:
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
        if not snapshot.verifier_established:
            return ["verify"]
        return ["approve", "reject"] if snapshot.dish_inspect_current else ["inspect"]
    if phase in {"await_submission", "ready_move_failed"}:
        if snapshot.live_status != "ready" or not snapshot.signoff_bound:
            return []
        if phase == "ready_move_failed":
            return ["repair-destination"] if snapshot.destination_repair_required else ["submit"]
        return list(snapshot.persisted_actions)
    if phase == "held_evidence":
        if snapshot.live_status != "pending-evidence":
            return []
        return list(snapshot.persisted_actions)
    if phase == "held_human":
        if snapshot.live_status != "pending-human-review":
            return []
        if snapshot.latest_cycle_outcome == "verification-hold":
            return ["resolved", "reopen"]
        if snapshot.latest_cycle_route == "human_review":
            return ["record-human-decision", "review-reject"]
        return []
    return list(snapshot.persisted_actions)


def action_is_legal(snapshot: WorkflowSnapshot, action: str) -> bool:
    return action in legal_actions(snapshot)
