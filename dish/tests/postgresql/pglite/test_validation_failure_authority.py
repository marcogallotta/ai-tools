"""PGlite boundary evidence for validation-only request persistence."""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import func, select, create_engine
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.postgres_service import PostgresRuntimeService
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from tests.support.postgresql.pglite_fixtures import seed_open_reservation, upgrade_on

pytestmark = pytest.mark.pglite


def _runtime(factory) -> PostgresRuntimeService:
    runtime = PostgresRuntimeService.__new__(PostgresRuntimeService)
    runtime._session_maker = factory
    runtime._cursor_secret = b"pglite-validation-replay-secret"
    return runtime


def _error() -> DishRuleError:
    return DishRuleError(
        "INVALID_ARGUMENT",
        "operation_id must be a canonical UUID",
        rule="uuid_identifier_required",
        details={"field": "operation_id"},
    )


def _count(session, model, request_id) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(model).where(model.request_id == request_id)
        )
        or 0
    )


def test_pglite_validation_failure_preserves_closed_reservation(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
        with session_scope(factory) as session:
            context, reserved_request_id, run_id, _payload_sha = seed_open_reservation(session)
            reservation = session.scalar(
                select(reservations.FirstRequestReservation).where(
                    reservations.FirstRequestReservation.request_id == reserved_request_id
                )
            )
            assert reservation is not None
            reservation_id = reservation.reservation_id
            before = {
                "state": reservation.state,
                "revision": reservation.reservation_revision,
                "consumed_at": reservation.consumed_at,
            }

        request_id = uuid.uuid4()
        runtime = _runtime(factory)
        principal = ServicePrincipal.from_values("owner-1", str(run_id))
        arguments = {"operation_id": "not-a-uuid"}
        first = runtime.record_replay_validation_failure(
            "create",
            arguments,
            principal=principal,
            request_id=str(request_id),
            error=_error(),
        )
        replay = runtime.record_replay_validation_failure(
            "create",
            arguments,
            principal=principal,
            request_id=str(request_id),
            error=_error(),
        )

        assert first["code"] == "INVALID_ARGUMENT"
        assert "request_replayed" not in first["data"]
        assert replay["data"]["request_replayed"] is True
        normalized = copy.deepcopy(replay)
        normalized["data"].pop("request_replayed")
        assert normalized == first
        with session_scope(factory) as session:
            control = session.get(rel.MutationAdmissionControl, context["generation_id"])
            reservation = session.get(reservations.FirstRequestReservation, reservation_id)
            assert control is not None and control.state == "closed"
            assert reservation is not None
            assert {
                "state": reservation.state,
                "revision": reservation.reservation_revision,
                "consumed_at": reservation.consumed_at,
            } == before
            assert _count(session, wf.ServiceRequest, request_id) == 1
            assert _count(session, wf.ServiceRequestOutcome, request_id) == 1
            assert _count(session, wf.CommandExecution, request_id) == 0
    finally:
        engine.dispose()
