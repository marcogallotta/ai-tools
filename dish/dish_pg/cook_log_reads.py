"""Shared PostgreSQL cook-log row selection and keyset paging.

This module owns only the database paging seam shared by Action/CLI and the
private frontend. Caller-specific identity, authentication, cursor encoding,
and response shapes remain outside this layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from . import stage3_models as wf


@dataclass(frozen=True, slots=True)
class CookLogReadRow:
    entry: wf.CookLogEntry
    execution: wf.CommandExecution
    request: wf.ServiceRequest


@dataclass(frozen=True, slots=True)
class CookLogReadPage:
    rows: tuple[CookLogReadRow, ...]
    has_more: bool


def read_cook_log_page(
    session: Session,
    *,
    generation_id: UUID,
    task_id: UUID,
    page_size: int,
    after_recorded_at: datetime | None = None,
    after_log_id: UUID | None = None,
) -> CookLogReadPage:
    """Read one oldest-first cook-log page with deterministic lookahead."""

    if not 1 <= page_size <= 100:
        raise ValueError("cook-log page_size must be between 1 and 100")
    if (after_recorded_at is None) != (after_log_id is None):
        raise ValueError("cook-log boundary must be complete")

    statement = (
        select(wf.CookLogEntry, wf.CommandExecution, wf.ServiceRequest)
        .join(
            wf.CommandExecution,
            wf.CommandExecution.execution_id == wf.CookLogEntry.command_execution_id,
        )
        .join(
            wf.ServiceRequest,
            wf.ServiceRequest.request_id == wf.CommandExecution.request_id,
        )
        .where(
            wf.CookLogEntry.generation_id == generation_id,
            wf.CookLogEntry.task_id == task_id,
        )
    )
    if after_recorded_at is not None and after_log_id is not None:
        statement = statement.where(
            or_(
                wf.CookLogEntry.recorded_at > after_recorded_at,
                and_(
                    wf.CookLogEntry.recorded_at == after_recorded_at,
                    wf.CookLogEntry.log_id > after_log_id,
                ),
            )
        )

    result = list(
        session.execute(
            statement.order_by(wf.CookLogEntry.recorded_at, wf.CookLogEntry.log_id).limit(
                page_size + 1
            )
        )
    )
    return CookLogReadPage(
        rows=tuple(
            CookLogReadRow(entry=entry, execution=execution, request=request)
            for entry, execution, request in result[:page_size]
        ),
        has_more=len(result) > page_size,
    )
