"""Native PostgreSQL validation-failure request/outcome replay evidence."""
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import func, select

from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.postgres_service import PostgresRuntimeService
from dish_pg.workflow import WorkflowAuthorityService, sha256_json
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from tests.support.postgresql.concurrency import run_concurrent_workers, wait_at_barrier
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next, core_db
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _runtime(factory) -> PostgresRuntimeService:
    runtime = PostgresRuntimeService.__new__(PostgresRuntimeService)
    runtime._session_maker = factory
    runtime._cursor_secret = b"native-validation-replay-secret!"
    return runtime


def _error(field: str = "operation_id") -> DishRuleError:
    return DishRuleError(
        "INVALID_ARGUMENT",
        f"{field} must be a canonical UUID",
        rule="uuid_identifier_required",
        details={"field": field},
    )


def _count(session, model, request_id) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(model).where(model.request_id == request_id)
        )
        or 0
    )


def _assert_one_validation_outcome(factory, request_id) -> None:
    with session_scope(factory) as session:
        assert _count(session, wf.ServiceRequest, request_id) == 1
        assert _count(session, wf.ServiceRequestOutcome, request_id) == 1
        assert _count(session, wf.CommandExecution, request_id) == 0
        assert _count(session, wf.GovernedAuditEvent, request_id) == 1
        assert _count(session, wf.InvocationAuditObligation, request_id) == 1


def _without_replay_metadata(payload):
    normalized = copy.deepcopy(payload)
    normalized["data"].pop("request_replayed", None)
    return normalized


def test_native_validation_failure_replays_one_authoritative_outcome(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        run_id = _next(ids)
        request_id = _next(ids)
        WorkflowAuthorityService(session).register_run(
            run_id=run_id,
            generation_id=context["generation_id"],
            owner_id="owner-1",
            agent="claude",
            capability_digest=run_id.bytes + run_id.bytes,
            registered_at=NOW,
        )

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

    assert "request_replayed" not in first["data"]
    assert replay["data"]["request_replayed"] is True
    assert _without_replay_metadata(replay) == first
    _assert_one_validation_outcome(factory, request_id)

    with session_scope(factory) as session:
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        assert outcome is not None
        authoritative_payload = copy.deepcopy(outcome.result_payload)
        authoritative_sha256 = outcome.result_sha256

    with pytest.raises(DishRuleError) as caught:
        runtime.record_replay_validation_failure(
            "create",
            arguments,
            principal=principal,
            request_id=str(request_id),
            error=_error("task_id"),
        )
    assert caught.value.code == "CONFLICT"
    assert caught.value.rule == "service_request_identity_conflict"

    with session_scope(factory) as session:
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        assert outcome is not None
        assert dict(outcome.result_payload) == authoritative_payload
        assert outcome.result_sha256 == authoritative_sha256
    _assert_one_validation_outcome(factory, request_id)


def test_native_concurrent_identical_validation_failures_converge(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        run_id = _next(ids)
        request_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)

    runtime = _runtime(factory)
    principal = ServicePrincipal.from_values("owner-1", str(run_id))
    arguments = {"operation_id": "not-a-uuid"}

    def record(_index, barrier):
        wait_at_barrier(barrier, checkpoint="native validation replay race")
        return runtime.record_replay_validation_failure(
            "create",
            arguments,
            principal=principal,
            request_id=str(request_id),
            error=_error(),
        )

    results = run_concurrent_workers(2, record)
    replay_flags = [result["data"].get("request_replayed") for result in results]
    assert sorted(replay_flags, key=lambda value: value is True) == [None, True]
    assert _without_replay_metadata(results[0]) == _without_replay_metadata(results[1])
    _assert_one_validation_outcome(factory, request_id)


def test_native_closed_admission_preserves_first_request_reservation(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        _service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        reservation_run_id = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=reservation_run_id,
            agent="service",
        )
        cutover_id = _next(ids)
        plan_id = _next(ids)
        reserved_request_id = _next(ids)
        reserved_payload = {"command": "start", "arguments": {"task_id": str(task_id)}}
        session.add(
            rel.CutoverRun(
                cutover_run_id=cutover_id,
                candidate_id=candidate_id,
                state="admission_open",
                state_revision=5,
                started_at=NOW,
                terminal_at=None,
            )
        )
        session.add(
            rel.FirstAdmissionPlan(
                plan_id=plan_id,
                cutover_run_id=cutover_id,
                request_id=reserved_request_id,
                command_name="start",
                task_id=task_id,
                expected_projection_events=1,
                payload=reserved_payload,
                plan_sha256=HASH_A,
                recorded_at=NOW,
            )
        )
        session.flush()
        reservation_id = _next(ids)
        session.add(
            reservations.FirstRequestReservation(
                reservation_id=reservation_id,
                plan_id=plan_id,
                cutover_run_id=cutover_id,
                candidate_id=candidate_id,
                generation_id=context["generation_id"],
                request_id=reserved_request_id,
                command_name="start",
                owner_id="owner-1",
                principal_class="service",
                run_id=reservation_run_id,
                canonical_payload_sha256=sha256_json(reserved_payload),
                state="reserved",
                reservation_revision=1,
                reserved_at=NOW,
                consumed_at=None,
            )
        )
        session.flush()

    runtime = _runtime(factory)
    result = runtime.record_replay_validation_failure(
        "create",
        {"operation_id": "not-a-uuid"},
        principal=ServicePrincipal.from_values("owner-1", str(run_id)),
        request_id=str(request_id),
        error=_error(),
    )
    assert result["code"] == "INVALID_ARGUMENT"

    with session_scope(factory) as session:
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        reservation = session.get(reservations.FirstRequestReservation, reservation_id)
        assert control is not None and control.state == "closed"
        assert reservation is not None
        assert reservation.state == "reserved"
        assert reservation.reservation_revision == 1
        assert reservation.consumed_at is None
    _assert_one_validation_outcome(factory, request_id)
