"""Bounded read-only facts for the operator-facing frontend admin prototype."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from dish_pg import stage3_models as workflow
from dish_pg.frontend_board_query import BoardReadUnavailable, CardFact, FrontendBoardQuery, SectionFact
from dish_pg.legacy_history_import import unresolved_legacy_attention


@dataclass(frozen=True, slots=True)
class AdminAuditFact:
    audit_event_id: UUID
    request_id: UUID
    command_execution_id: UUID | None
    task_id: UUID
    operation_id: UUID | None
    event_type: str
    actor: str
    occurred_at: object


@dataclass(frozen=True, slots=True)
class AdminHumanReviewFact:
    requirement_id: UUID
    task_id: UUID
    operation_id: UUID
    cycle_id: UUID | None
    route: str
    question: str
    opened_at: object


@dataclass(frozen=True, slots=True)
class FrontendAdminFacts:
    sections: tuple[SectionFact, ...]
    cards: tuple[CardFact, ...]
    events: tuple[AdminAuditFact, ...]
    evaluation_time: object
    human_reviews: tuple[AdminHumanReviewFact, ...] = ()
    legacy_attentions: tuple[dict[str, object], ...] = ()


class FrontendAdminQuery:
    """Capture current operator facts without deriving workflow authority."""

    def __init__(self, session: Session):
        self.session = session
        self.board_query = FrontendBoardQuery(session)

    def capture(
        self,
        *,
        projection_delay: timedelta,
        max_cards: int = 5000,
        max_events: int = 120,
    ) -> FrontendAdminFacts:
        if max_cards <= 0 or max_events <= 0:
            raise ValueError("admin read bounds must be positive")
        registry = self.board_query.bootstrap_registry()
        context = registry.context
        cards = self.board_query.active_cards(
            registry=registry,
            projection_delay=projection_delay,
            max_cards=max_cards,
        )
        task_ids = [card.task_id for card in cards]
        events: tuple[AdminAuditFact, ...] = ()
        human_reviews: tuple[AdminHumanReviewFact, ...] = ()
        if task_ids:
            event_rows = self.session.execute(
                select(
                    workflow.GovernedAuditEvent.audit_event_id,
                    workflow.GovernedAuditEvent.request_id,
                    workflow.GovernedAuditEvent.command_execution_id,
                    workflow.GovernedAuditEvent.task_id,
                    workflow.GovernedAuditEvent.operation_id,
                    workflow.GovernedAuditEvent.event_type,
                    workflow.GovernedAuditEvent.actor,
                    workflow.GovernedAuditEvent.occurred_at,
                )
                .where(
                    workflow.GovernedAuditEvent.generation_id == context.generation_id,
                    workflow.GovernedAuditEvent.task_id.in_(task_ids),
                )
                .order_by(
                    workflow.GovernedAuditEvent.occurred_at.desc(),
                    workflow.GovernedAuditEvent.audit_event_id.desc(),
                )
                .limit(max_events)
            ).mappings()
            events = tuple(AdminAuditFact(**dict(row)) for row in event_rows if row["task_id"] is not None)
            human_review_rows = self.session.execute(
                select(
                    workflow.HumanReviewRequirement.requirement_id,
                    workflow.HumanReviewRequirement.task_id,
                    workflow.HumanReviewRequirement.operation_id,
                    workflow.HumanReviewRequirement.cycle_id,
                    workflow.HumanReviewRequirement.route,
                    workflow.HumanReviewRequirement.question,
                    workflow.HumanReviewRequirement.opened_at,
                )
                .join(
                    workflow.WorkflowOperation,
                    and_(
                        workflow.WorkflowOperation.operation_id == workflow.HumanReviewRequirement.operation_id,
                        workflow.WorkflowOperation.generation_id == workflow.HumanReviewRequirement.generation_id,
                        workflow.WorkflowOperation.task_id == workflow.HumanReviewRequirement.task_id,
                    ),
                )
                .where(
                    workflow.HumanReviewRequirement.generation_id == context.generation_id,
                    workflow.HumanReviewRequirement.task_id.in_(task_ids),
                    workflow.HumanReviewRequirement.route == "human_review",
                    workflow.HumanReviewRequirement.state == "open",
                    workflow.WorkflowOperation.lifecycle == "open",
                )
                .order_by(
                    workflow.HumanReviewRequirement.opened_at.desc(),
                    workflow.HumanReviewRequirement.requirement_id.desc(),
                )
            ).mappings()
            human_reviews = tuple(AdminHumanReviewFact(**dict(row)) for row in human_review_rows)
        return FrontendAdminFacts(
            sections=registry.sections,
            cards=cards,
            events=events,
            human_reviews=human_reviews,
            evaluation_time=context.evaluation_time,
            legacy_attentions=tuple(unresolved_legacy_attention(self.session, context.generation_id)),
        )
