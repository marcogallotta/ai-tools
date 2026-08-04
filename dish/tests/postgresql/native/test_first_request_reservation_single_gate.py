"""Native PostgreSQL coverage for single-use first-request admission."""
from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.workflow import RequestSpec, WorkflowAuthorityService, sha256_json
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _next, core_db
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import NOW, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _seed(factory, ids, *, state: str = "reserved"):
    payload = {"command": "start", "arguments": {"task_id": "fixture"}}
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task = _import_one(session, ids, context)
        _service, candidate_id = _prepare_candidate(session, ids, context, task.task_id)
        cutover_id = _next(ids)
        request_id = _next(ids)
        run_id = _next(ids)
        plan_id = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner="owner-1",
            agent="service",
        )
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
                request_id=request_id,
                command_name="start",
                task_id=task.task_id,
                expected_projection_events=1,
                payload=payload,
                plan_sha256=HASH_A,
                recorded_at=NOW,
            )
        )
        session.flush()
        session.add(
            reservations.FirstRequestReservation(
                reservation_id=_next(ids),
                plan_id=plan_id,
                cutover_run_id=cutover_id,
                candidate_id=candidate_id,
                generation_id=context["generation_id"],
                request_id=request_id,
                command_name="start",
                owner_id="owner-1",
                principal_class="service",
                run_id=run_id,
                canonical_payload_sha256=sha256_json(payload),
                state=state,
                reservation_revision=1,
                reserved_at=NOW,
                consumed_at=None,
            )
        )
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None
        control.state = "open"
        control.control_revision += 1
        control.opened_at = NOW
        control.updated_at = NOW
    return context["generation_id"], request_id, run_id, payload


def _spec(*, request_id, generation_id, run_id, payload, owner="owner-1") -> RequestSpec:
    return RequestSpec(
        request_id=request_id,
        generation_id=generation_id,
        run_id=run_id,
        owner_id=owner,
        principal_class="service",
        command_name="start",
        canonical_payload=payload,
        protocol_release="protocol-1",
        dish_release="dish-test",
        admitted_at=NOW,
    )


def test_native_exact_reserved_first_request_succeeds(core_db) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed(factory, ids)
    with session_scope(factory) as session:
        admission = WorkflowAuthorityService(session).admit_request(
            _spec(
                request_id=request_id,
                generation_id=generation_id,
                run_id=run_id,
                payload=payload,
            )
        )
        assert not admission.replayed
    with Session(factory.kw["bind"]) as session:
        reservation = session.scalar(select(reservations.FirstRequestReservation))
        assert reservation is not None
        assert reservation.state == "consumed"
        assert reservation.reservation_revision == 2


def _consume_first(factory, generation_id, request_id, run_id, payload) -> RequestSpec:
    first_spec = _spec(
        request_id=request_id,
        generation_id=generation_id,
        run_id=run_id,
        payload=payload,
    )
    with session_scope(factory) as session:
        first = WorkflowAuthorityService(session).admit_request(first_spec)
        assert not first.replayed
    return first_spec


def test_native_unrelated_valid_second_request_succeeds(core_db) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed(factory, ids)
    _consume_first(factory, generation_id, request_id, run_id, payload)

    second_run_id = _next(ids)
    second_request_id = _next(ids)
    second_payload = {"command": "start", "arguments": {"task_id": "second"}}
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=generation_id,
            run_id=second_run_id,
            owner="owner-1",
            agent="service",
        )
        second = WorkflowAuthorityService(session).admit_request(
            _spec(
                request_id=second_request_id,
                generation_id=generation_id,
                run_id=second_run_id,
                payload=second_payload,
            )
        )
        assert not second.replayed


def test_native_first_request_replay_succeeds(core_db) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed(factory, ids)
    first_spec = _consume_first(factory, generation_id, request_id, run_id, payload)
    with session_scope(factory) as session:
        replay = WorkflowAuthorityService(session).admit_request(first_spec)
        assert replay.replayed
        assert replay.request.request_id == request_id


def test_native_mismatched_request_before_consumption_fails(core_db) -> None:
    factory, ids = core_db
    generation_id, _request_id, run_id, payload = _seed(factory, ids)
    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="does not match the reserved request",
        ):
            raw.execute(
                """INSERT INTO service_requests (
                    request_id,generation_id,run_id,owner_id,principal_class,
                    command_name,canonical_payload_sha256,canonical_payload,
                    protocol_release,dish_release,admitted_at
                ) VALUES (%s,%s,%s,'owner-1','service','start',%s,%s::json,
                          'protocol-1','dish-test',%s)""",
                (
                    uuid.uuid4(),
                    generation_id,
                    run_id,
                    sha256_json(payload),
                    '{"command":"start","arguments":{"task_id":"fixture"}}',
                    NOW,
                ),
            )
    finally:
        raw.close()


def test_native_cancelled_reservation_fails_closed(core_db) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed(factory, ids, state="cancelled")
    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="reservation is not consumable",
        ):
            raw.execute(
                """INSERT INTO service_requests (
                    request_id,generation_id,run_id,owner_id,principal_class,
                    command_name,canonical_payload_sha256,canonical_payload,
                    protocol_release,dish_release,admitted_at
                ) VALUES (%s,%s,%s,'owner-1','service','start',%s,%s::json,
                          'protocol-1','dish-test',%s)""",
                (
                    request_id,
                    generation_id,
                    run_id,
                    sha256_json(payload),
                    '{"command":"start","arguments":{"task_id":"fixture"}}',
                    NOW,
                ),
            )
    finally:
        raw.close()
