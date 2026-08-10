"""Bounded read-only facts for the operator-facing frontend admin prototype."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from dish_pg import stage3_models as workflow
from dish_pg.frontend_board_query import BoardReadUnavailable, CardFact, FrontendBoardQuery, SectionFact


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
class FrontendAdminFacts:
    sections: tuple[SectionFact, ...]
    cards: tuple[CardFact, ...]
    events: tuple[AdminAuditFact, ...]
    evaluation_time: object


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
        return FrontendAdminFacts(
            sections=registry.sections,
            cards=cards,
            events=events,
            evaluation_time=context.evaluation_time,
        )
