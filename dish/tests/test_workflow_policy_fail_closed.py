from __future__ import annotations

from dataclasses import replace

import pytest

from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions


def _verification_snapshot() -> WorkflowSnapshot:
    return WorkflowSnapshot(
        operation_status="open",
        operation_phase="await_verification",
        operation_kind="initial",
        persisted_actions=("verify",),
        live_status="pending-verification",
        live_section_gid="vq",
        verification_queue_gid="vq",
        cycle_reviewed=False,
        latest_cycle_outcome=None,
        latest_cycle_route=None,
        validation_rules=(),
        required_cycle_exists=True,
    )


def _submission_snapshot() -> WorkflowSnapshot:
    return WorkflowSnapshot(
        operation_status="open",
        operation_phase="await_submission",
        operation_kind="initial",
        persisted_actions=("submit",),
        live_status="ready",
        live_section_gid="vq",
        verification_queue_gid="vq",
        cycle_reviewed=True,
        latest_cycle_outcome="approved",
        latest_cycle_route="none",
        validation_rules=(),
        signoff_bound=True,
    )


@pytest.mark.invariant_workflow_action_authority
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("baseline", "changes"),
    [
        pytest.param(_verification_snapshot, {"pending_steps": ("route_cycle",)}, id="pending-step"),
        pytest.param(_verification_snapshot, {"unresolved_attempts": ("write:attempt",)}, id="unresolved-effect"),
        pytest.param(_verification_snapshot, {"migration_reconciliation_required": True}, id="migration-reconciliation"),
        pytest.param(_verification_snapshot, {"identity_matches": False}, id="identity-drift"),
        pytest.param(_verification_snapshot, {"placement_matches": False}, id="placement-drift"),
        pytest.param(_verification_snapshot, {"held_baseline_matches": False}, id="held-baseline-drift"),
        pytest.param(_verification_snapshot, {"required_cycle_exists": False}, id="missing-cycle"),
        pytest.param(_verification_snapshot, {"live_status": "pending-research"}, id="verification-status"),
        pytest.param(_verification_snapshot, {"live_section_gid": "rq"}, id="verification-placement"),
        pytest.param(_verification_snapshot, {"validation_rules": ("state.illegal-combination",)}, id="invalid-document"),
        pytest.param(_submission_snapshot, {"signoff_bound": False}, id="missing-signoff-binding"),
        pytest.param(_submission_snapshot, {"live_status": "pending-verification"}, id="submission-status"),
    ],
)
def test_each_unsafe_authority_fact_suppresses_all_actions(baseline, changes):
    snapshot = replace(baseline(), **changes)

    assert legal_actions(snapshot) == []
