"""Bounded factual reads for Stage 4 frontend task detail.

The query captures durable PostgreSQL facts only. Presentation, rendering and
advisory text are derived after the read transaction closes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, case, literal, select
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg import stage3_models as workflow
from dish_pg.frontend_board_query import FrontendBoardQuery
from dish_pg.frontend_projection_query import ProjectionFact, projection_fact


class TaskDetailIneligible(LookupError):
    """The task exists but is not eligible for the active frontend board."""


@dataclass(frozen=True, slots=True)
class LeaseFact:
    state: str
    actor_role: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerificationFact:
    lifecycle: str
    outcome: str | None


@dataclass(frozen=True, slots=True)
class HoldFact:
    kind: str
    state: str


@dataclass(frozen=True, slots=True)
class AbandonmentFact:
    state: str


@dataclass(frozen=True, slots=True)
class DetailFacts:
    task_id: UUID
    evaluation_time: datetime
    title: str
    body: str
    existence_state: str
    section_label: str
    section_workflow_role: str
    project_label: str
    operation_kind: str | None
    operation_phase: str | None
    isolated: bool
    lease_attention: bool
    verification_attention: bool
    hold_active: bool
    recovery_required: bool
    abandonment_active: bool
    succession_active: bool
    projection_abnormal: bool
    lease: LeaseFact | None
    verification: VerificationFact | None
    holds: tuple[HoldFact, ...]
    abandonment: AbandonmentFact | None
    projection: ProjectionFact


class FrontendDetailQuery:
    """Capture one immutable detail fact bundle from the active PG generation."""

    def __init__(self, session: Session):
        self.session = session
        self.board = FrontendBoardQuery(session)

    def route_candidate_ids(self, *, limit: int) -> tuple[UUID, ...]:
        if limit <= 0:
            raise ValueError("route candidate limit must be positive")
        rows = self.session.scalars(
            select(models.DishTask.task_id).order_by(models.DishTask.task_id).limit(limit + 1)
        )
        return tuple(rows)

    def capture(self, *, task_id: UUID, projection_delay: timedelta) -> DetailFacts:
        if projection_delay <= timedelta(0):
            raise ValueError("projection_delay must be positive")
        context = self.board.context()
        row = self.session.execute(
            self._detail_statement(
                generation_id=context.generation_id,
                registry_version_id=context.registry_version_id,
                evaluation_time=context.evaluation_time,
                task_id=task_id,
                projection_delay=projection_delay,
            )
        ).mappings().one_or_none()
        if row is None:
            raise TaskDetailIneligible("task is not eligible for the active frontend registry")
        operation_id = row["operation_id"]
        return DetailFacts(
            task_id=task_id,
            evaluation_time=context.evaluation_time,
            title=row["title"],
            body=row["body"],
            existence_state=row["existence_state"],
            section_label=row["section_label"],
            section_workflow_role=row["section_workflow_role"],
            project_label=row["project_label"],
            operation_kind=row["operation_kind"],
            operation_phase=row["operation_phase"],
            isolated=bool(row["isolated"]),
            lease_attention=bool(row["lease_attention"]),
            verification_attention=bool(row["verification_attention"]),
            hold_active=bool(row["hold_active"]),
            recovery_required=bool(row["recovery_required"]),
            abandonment_active=bool(row["abandonment_active"]),
            succession_active=bool(row["succession_active"]),
            projection_abnormal=bool(row["projection_abnormal"]),
            lease=self._lease(operation_id),
            verification=self._verification(operation_id),
            holds=self._holds(context.generation_id, task_id),
            abandonment=self._abandonment(context.generation_id, task_id),
            projection=projection_fact(
                self.session,
                generation_id=context.generation_id,
                task_id=task_id,
                evaluation_time=context.evaluation_time,
                projection_delay=projection_delay,
            ),
        )

    def _detail_statement(
        self,
        *,
        generation_id: UUID,
        registry_version_id: UUID,
        evaluation_time: datetime,
        task_id: UUID,
        projection_delay: timedelta,
    ):
        attention = self.board.attention_columns(
            generation_id=generation_id,
            task_id=models.DishTask.task_id,
            evaluation_time=evaluation_time,
            projection_delay=projection_delay,
        )
        return (
            select(
                models.DishTask.existence_state,
                models.ContentVersion.title,
                models.ContentVersion.body,
                case(
                    (
                        and_(
                            models.SectionRegistryEntry.display_name.like("Imported section %"),
                            models.SectionRegistryEntry.workflow_role == "research_queue",
                        ),
                        literal("Research Queue"),
                    ),
                    (
                        and_(
                            models.SectionRegistryEntry.display_name.like("Imported section %"),
                            models.SectionRegistryEntry.workflow_role == "verification_queue",
                        ),
                        literal("Verification Queue"),
                    ),
                    else_=models.SectionRegistryEntry.display_name,
                ).label("section_label"),
                models.SectionRegistryEntry.workflow_role.label("section_workflow_role"),
                models.GovernedProject.logical_name.label("project_label"),
                workflow.WorkflowOperation.operation_id,
                workflow.WorkflowOperation.kind.label("operation_kind"),
                workflow.WorkflowOperation.phase.label("operation_phase"),
                (models.DishTask.existence_state == "isolated").label("isolated"),
                *(expr.label(name) for name, expr in attention.items()),
            )
            .select_from(models.DishTask)
            .join(
                models.DishState,
                and_(
                    models.DishState.generation_id == generation_id,
                    models.DishState.task_id == models.DishTask.task_id,
                ),
            )
            .join(
                models.ContentVersion,
                and_(
                    models.ContentVersion.generation_id == generation_id,
                    models.ContentVersion.task_id == models.DishTask.task_id,
                    models.ContentVersion.content_version_id
                    == models.DishState.current_content_version_id,
                ),
            )
            .join(
                models.SectionRegistryEntry,
                and_(
                    models.SectionRegistryEntry.registry_version_id == registry_version_id,
                    models.SectionRegistryEntry.section_id
                    == models.DishState.section_id,
                ),
            )
            .join(
                models.GovernedSection,
                models.GovernedSection.section_id == models.DishState.section_id,
            )
            .join(models.GovernedProject, models.GovernedProject.project_id == models.GovernedSection.project_id)
            .join(
                models.CurrentTaskProjectMembership,
                and_(
                    models.CurrentTaskProjectMembership.generation_id == generation_id,
                    models.CurrentTaskProjectMembership.task_id == models.DishTask.task_id,
                    models.CurrentTaskProjectMembership.project_id == models.GovernedSection.project_id,
                    models.CurrentTaskProjectMembership.is_member.is_(True),
                ),
            )
            .outerjoin(
                workflow.WorkflowOperation,
                and_(
                    workflow.WorkflowOperation.generation_id == generation_id,
                    workflow.WorkflowOperation.task_id == models.DishTask.task_id,
                    workflow.WorkflowOperation.lifecycle == "open",
                ),
            )
            .where(
                models.DishTask.task_id == task_id,
                models.DishTask.existence_state.in_(("ordinary", "isolated")),
                models.DishState.completed.is_(False),
                models.GovernedSection.lifecycle == "active",
                models.GovernedProject.lifecycle == "active",
            )
        )

    def _lease(self, operation_id: UUID | None) -> LeaseFact | None:
        if operation_id is None:
            return None
        row = self.session.execute(
            select(
                workflow.ServiceLease.state,
                workflow.ServiceLease.actor_role,
                workflow.ServiceLease.expires_at,
            )
            .where(
                workflow.ServiceLease.operation_id == operation_id,
                workflow.ServiceLease.lease_kind == "actor",
            )
            .order_by(workflow.ServiceLease.actor_attempt_sequence.desc())
            .limit(1)
        ).mappings().one_or_none()
        return None if row is None else LeaseFact(**dict(row))

    def _verification(self, operation_id: UUID | None) -> VerificationFact | None:
        if operation_id is None:
            return None
        row = self.session.execute(
            select(workflow.VerificationCycle.lifecycle, workflow.VerificationCycle.outcome)
            .where(workflow.VerificationCycle.operation_id == operation_id)
            .order_by(workflow.VerificationCycle.cycle_sequence.desc())
            .limit(1)
        ).mappings().one_or_none()
        return None if row is None else VerificationFact(**dict(row))

    def _holds(self, generation_id: UUID, task_id: UUID) -> tuple[HoldFact, ...]:
        evidence = self.session.scalar(
            select(workflow.EvidenceHold.state)
            .where(
                workflow.EvidenceHold.generation_id == generation_id,
                workflow.EvidenceHold.task_id == task_id,
                workflow.EvidenceHold.state == "open",
            )
            .order_by(workflow.EvidenceHold.opened_at.desc())
            .limit(1)
        )
        review = self.session.scalar(
            select(workflow.HumanReviewRequirement.state)
            .where(
                workflow.HumanReviewRequirement.generation_id == generation_id,
                workflow.HumanReviewRequirement.task_id == task_id,
                workflow.HumanReviewRequirement.route == "two_pass_hold",
                workflow.HumanReviewRequirement.state == "open",
            )
            .order_by(workflow.HumanReviewRequirement.opened_at.desc())
            .limit(1)
        )
        facts: list[HoldFact] = []
        if evidence is not None:
            facts.append(HoldFact(kind="evidence", state=evidence))
        if review is not None:
            facts.append(HoldFact(kind="two_pass", state=review))
        return tuple(facts)

    def _abandonment(self, generation_id: UUID, task_id: UUID) -> AbandonmentFact | None:
        state = self.session.scalar(
            select(workflow.AbandonmentAttempt.state)
            .where(
                workflow.AbandonmentAttempt.generation_id == generation_id,
                workflow.AbandonmentAttempt.task_id == task_id,
                workflow.AbandonmentAttempt.state.in_(("preparing", "published", "blocked", "reconciling")),
            )
            .order_by(workflow.AbandonmentAttempt.created_at.desc())
            .limit(1)
        )
        return None if state is None else AbandonmentFact(state=state)
