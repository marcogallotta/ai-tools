"""Bounded projection fact capture for frontend detail presentation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dish_pg import stage5_models as projection


@dataclass(frozen=True, slots=True)
class ProjectionFact:
    drifted_at: datetime | None
    blocked_at: datetime | None
    uncertain_at: datetime | None
    delayed_at: datetime | None


def projection_fact(
    session: Session,
    *,
    generation_id: UUID,
    task_id: UUID,
    evaluation_time: datetime,
    projection_delay: timedelta,
) -> ProjectionFact:
    active_external_epoch = session.scalar(
        select(projection.ProjectionEpoch.projection_epoch_id)
        .where(
            projection.ProjectionEpoch.generation_id == generation_id,
            projection.ProjectionEpoch.status == "active",
            projection.ProjectionEpoch.external_effects_enabled.is_(True),
        )
        .limit(1)
    )
    if active_external_epoch is None:
        return ProjectionFact(None, None, None, None)

    cutoff = evaluation_time - projection_delay
    statement = select(
        select(func.max(projection.ProjectionDriftEvent.detected_at))
        .where(
            projection.ProjectionDriftEvent.generation_id == generation_id,
            projection.ProjectionDriftEvent.task_id == task_id,
            projection.ProjectionDriftEvent.state == "open",
        )
        .scalar_subquery()
        .label("drifted_at"),
        select(func.max(projection.ProjectionOutboxEvent.created_at))
        .where(
            projection.ProjectionOutboxEvent.generation_id == generation_id,
            projection.ProjectionOutboxEvent.task_id == task_id,
            projection.ProjectionOutboxEvent.origin == "live",
            projection.ProjectionOutboxEvent.state == "blocked",
        )
        .scalar_subquery()
        .label("blocked_at"),
        select(func.max(projection.ProjectionOutboxEvent.created_at))
        .where(
            projection.ProjectionOutboxEvent.generation_id == generation_id,
            projection.ProjectionOutboxEvent.task_id == task_id,
            projection.ProjectionOutboxEvent.origin == "live",
            projection.ProjectionOutboxEvent.state == "uncertain",
        )
        .scalar_subquery()
        .label("uncertain_at"),
        select(func.max(projection.ProjectionOutboxEvent.created_at))
        .where(
            projection.ProjectionOutboxEvent.generation_id == generation_id,
            projection.ProjectionOutboxEvent.task_id == task_id,
            projection.ProjectionOutboxEvent.origin == "live",
            projection.ProjectionOutboxEvent.state.in_(("pending", "claimed")),
            projection.ProjectionOutboxEvent.created_at <= cutoff,
        )
        .scalar_subquery()
        .label("delayed_at"),
    )
    return ProjectionFact(**dict(session.execute(statement).mappings().one()))
