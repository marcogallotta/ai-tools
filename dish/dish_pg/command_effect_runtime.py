"""Runtime boundary for PostgreSQL command-effect intent and verification.

The descriptive command-effect contract lives in :mod:`dish_pg.command_effects`.
This module owns the persistence-facing seam that records projection intent and
verifies the committed rows produced by handlers.  It does not commit; callers
retain transaction ownership so a mismatch can roll back the complete command.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from . import stage5_models as projection
from . import stage6_models as release
from .command_effects import CommandEffectSpec


POST_BURN_CUTOVER_STATES = (
    "rollback_burned",
    "admission_open",
    "first_admission_verified",
    "completed",
)


def external_projection_required(session: Session, *, generation_id: uuid.UUID) -> bool:
    """Return whether live commands still owe an external projection intent.

    Rollback burn is the durable end boundary for Asana projection. After
    that state is persisted, PostgreSQL command success must be self-contained
    and must not create new external-projection debt.
    """

    retired = session.scalar(
        select(release.CutoverRun.cutover_run_id)
        .join(
            release.ReleaseCandidate,
            release.ReleaseCandidate.candidate_id == release.CutoverRun.candidate_id,
        )
        .where(
            release.ReleaseCandidate.generation_id == generation_id,
            release.CutoverRun.state.in_(POST_BURN_CUTOVER_STATES),
        )
        .limit(1)
    )
    return retired is None


class CommandEffectMismatch(RuntimeError):
    """Committed handler effects disagree with the authoritative command specification."""


class ProjectionAuthority(Protocol):
    """Narrow persistence authority used by the PostgreSQL command boundary."""

    def record(
        self,
        *,
        generation_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        event_type: str,
        payload: Mapping[str, Any],
        origin: str,
        created_at: datetime,
    ) -> uuid.UUID: ...

    def recover(
        self,
        *,
        attempt_id: uuid.UUID,
        route: str,
        arguments: Mapping[str, Any],
        actor: str,
        recovered_at: datetime,
        expected_task_id: uuid.UUID | None = None,
    ) -> Mapping[str, Any]: ...

    def unresolved_attempt_id(self, task_id: uuid.UUID) -> uuid.UUID | None: ...

    def task_freshness(self, task_id: uuid.UUID) -> Mapping[str, Any]: ...


def record_projection_intent(
    recorder: ProjectionAuthority,
    *,
    generation_id: uuid.UUID,
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    event_type: str,
    payload: Mapping[str, Any],
    origin: str,
    created_at: datetime,
) -> str:
    """Record one durable projection intent without taking transaction ownership."""

    return str(
        recorder.record(
            generation_id=generation_id,
            execution_id=execution_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            origin=origin,
            created_at=created_at,
        )
    )


def assert_committed_command_effects(
    session: Session,
    *,
    command_name: str,
    arguments: Mapping[str, Any],
    now: datetime,
    execution: wf.CommandExecution,
    task: models.DishTask | None,
    operation: wf.WorkflowOperation | None,
    expected: CommandEffectSpec,
    result_data: Mapping[str, Any],
    projection_origin: str = "live",
) -> None:
    """Fail closed when one handler's durable effects drift from its specification."""

    projection_types = tuple(
        session.scalars(
            select(projection.ProjectionOutboxEvent.event_type)
            .where(
                projection.ProjectionOutboxEvent.command_execution_id
                == execution.execution_id
            )
            .order_by(projection.ProjectionOutboxEvent.aggregate_sequence)
        ).all()
    )
    expected_projection_types = expected.projection_event_types
    if projection_origin == "live" and not external_projection_required(
        session, generation_id=execution.generation_id
    ):
        expected_projection_types = ()
    if projection_types != expected_projection_types:
        raise CommandEffectMismatch(
            f"{command_name} projection effects mismatch: "
            f"expected {expected_projection_types!r}, observed {projection_types!r}"
        )

    if not expected.verify_mutation_effects:
        return
    if task is None:
        raise CommandEffectMismatch(
            f"{command_name} effect verification requires task authority"
        )

    observed: set[str] = set()
    execution_id = execution.execution_id
    scalar_receipt = session.scalar(
        select(models.DishMutationReceipt).where(
            models.DishMutationReceipt.command_execution_id == execution_id
        )
    )
    if scalar_receipt is not None and scalar_receipt.completion_changed:
        observed.add("set_completion")
    if scalar_receipt is not None and scalar_receipt.archive_changed:
        observed.add("archive_task")
    if scalar_receipt is not None and scalar_receipt.content_changed:
        observed.add(
            "activate_corrected_content_version"
            if command_name in {"approve", "reject", "apply-proposal"}
            else "activate_content_version"
        )
    if scalar_receipt is not None and scalar_receipt.placement_changed:
        observed.add(
            "place_research_queue"
            if "place_research_queue" in expected.mutation_kinds
            else "place_verification_queue"
        )
    if session.scalar(
        select(wf.OperationStep.step_id).where(
            wf.OperationStep.command_execution_id == execution_id
        )
    ) is not None:
        observed.add("append_operation_step")
    if session.scalar(
        select(wf.VerificationCycle.cycle_id).where(
            wf.VerificationCycle.created_by_execution_id == execution_id
        )
    ) is not None:
        observed.add("open_verification_cycle")
    if session.scalar(
        select(wf.VerificationCorrection.correction_id).where(
            wf.VerificationCorrection.command_execution_id == execution_id
        )
    ) is not None:
        observed.add("record_verification_correction")
    if session.scalar(
        select(wf.VerificationSignoff.signoff_id).where(
            wf.VerificationSignoff.command_execution_id == execution_id
        )
    ) is not None:
        observed.add("record_verification_signoff")
    if session.scalar(
        select(wf.EvidenceHold.hold_id).where(
            wf.EvidenceHold.opened_by_execution_id == execution_id
        )
    ) is not None:
        observed.add("open_evidence_hold")
    if session.scalar(
        select(wf.HumanReviewRequirement.requirement_id).where(
            wf.HumanReviewRequirement.opened_by_execution_id == execution_id
        )
    ) is not None:
        observed.add("open_human_review")

    if operation is None:
        expected_mutations = set(expected.mutation_kinds)
        if observed != expected_mutations:
            raise CommandEffectMismatch(
                f"{command_name} authoritative effects mismatch: "
                f"expected {sorted(expected_mutations)!r}, observed {sorted(observed)!r}"
            )
        return

    if command_name == "reject":
        rejected_cycle = session.scalar(
            select(wf.VerificationCycle.cycle_id).where(
                wf.VerificationCycle.operation_id == operation.operation_id,
                wf.VerificationCycle.lifecycle == "rejected",
                wf.VerificationCycle.outcome.in_(("rejected", "verification-hold")),
                wf.VerificationCycle.terminal_at == now,
            )
        )
        if rejected_cycle is not None:
            observed.add("reject_verification_cycle")

    expected_phase = {
        "prepare": (
            "completed"
            if result_data.get("handoff") in {"checked-in", "planning-to-research"}
            else "await_verification"
        ),
        "approve": "await_submission",
        "hold-reject": "held_evidence",
        "reject": (
            "held_human"
            if result_data.get("verification_hold")
            or result_data.get("semantic_proposal_queued")
            else {
                "large": "await_verification",
                "evidence": "held_evidence",
                "human-review": "held_human",
                "human_review": "held_human",
            }.get(str(arguments.get("route", "large")), "held_human")
        ),
        "apply-proposal": "await_verification",
    }[command_name]
    if operation.phase == expected_phase:
        observed.add("advance_operation")

    expected_mutations = set(expected.mutation_kinds)
    if observed != expected_mutations:
        raise CommandEffectMismatch(
            f"{command_name} authoritative effects mismatch: "
            f"expected {sorted(expected_mutations)!r}, observed {sorted(observed)!r}"
        )
