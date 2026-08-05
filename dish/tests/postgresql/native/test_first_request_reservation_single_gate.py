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
from dish_pg.workflow import (
    MutationAdmissionClosed,
    RequestSpec,
    WorkflowAuthorityService,
    sha256_json,
)
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _next, core_db
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import NOW, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _seed(
    factory, ids, *, state: str = "reserved", cutover_state: str = "admission_open"
):
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
        if cutover_state not in {"rollback_burned", "admission_open"}:
            raise AssertionError(f"unsupported seeded cutover state: {cutover_state}")
        session.add(
            rel.CutoverRun(
                cutover_run_id=cutover_id,
                candidate_id=candidate_id,
                state=cutover_state,
                state_revision=4 if cutover_state == "rollback_burned" else 5,
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
                state="reserved",
                reservation_revision=1,
                reserved_at=NOW,
                consumed_at=None,
            )
        )
        session.flush()
        reservation = session.scalar(select(reservations.FirstRequestReservation))
        assert reservation is not None
        if state == "cancelled":
            reservation.state = "cancelled"
            reservation.reservation_revision = 2
        elif state != "reserved":
            raise AssertionError(f"unsupported seeded state: {state}")
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None
        assert control.state == "closed"
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


def test_native_exact_first_request_fails_before_first_request_gate(core_db) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed(
        factory, ids, cutover_state="rollback_burned"
    )
    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="closed pending first-request gate",
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


def test_native_unrelated_valid_second_request_fails_before_verification(core_db) -> None:
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
        with pytest.raises(
            MutationAdmissionClosed,
            match="pending first-admission verification",
        ):
            WorkflowAuthorityService(session).admit_request(
                _spec(
                    request_id=second_request_id,
                    generation_id=generation_id,
                    run_id=second_run_id,
                    payload=second_payload,
                )
            )


def test_native_direct_sql_cannot_open_general_admission_before_verification(core_db) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed(factory, ids)
    _consume_first(factory, generation_id, request_id, run_id, payload)

    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="mutation admission opens only after verified first admission",
        ):
            raw.execute(
                """UPDATE mutation_admission_controls
                      SET state = 'open',
                          control_revision = control_revision + 1,
                          opened_at = %s,
                          updated_at = %s
                    WHERE generation_id = %s""",
                (NOW, NOW, generation_id),
            )
    finally:
        raw.close()


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
            match="closed pending first-admission verification",
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


def test_native_missing_control_row_fails_closed(core_db) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed(factory, ids)
    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        raw.execute(
            "DELETE FROM mutation_admission_controls WHERE generation_id = %s",
            (generation_id,),
        )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="mutation admission is closed",
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


def test_native_initial_state_insert_guards_reject_direct_sql(core_db) -> None:
    factory, ids = core_db
    _seed(factory, ids)
    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="release candidate must initially be assembling at revision 1",
        ):
            raw.execute(
                """INSERT INTO release_candidates (
                    candidate_id,generation_id,source_import_batch_id,shadow_baseline_id,
                    projection_epoch_id,source_release,source_commit,ledger_through_commit,
                    schema_head,dish_release,honest_release,protocol_release,openapi_release,
                    routing_release,status,candidate_revision,validation_bundle_sha256,
                    created_at,validated_at,approved_at,terminal_at
                )
                SELECT %s,generation_id,source_import_batch_id,shadow_baseline_id,
                       projection_epoch_id,source_release,source_commit,ledger_through_commit,
                       schema_head,dish_release,honest_release,protocol_release,openapi_release,
                       routing_release,'validated',1,validation_bundle_sha256,
                       created_at,validated_at,NULL,NULL
                  FROM release_candidates LIMIT 1""",
                (uuid.uuid4(),),
            )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="mutation admission control must initially be closed at revision 1",
        ):
            raw.execute(
                """INSERT INTO mutation_admission_controls (
                    generation_id,candidate_id,state,control_revision,opened_at,updated_at
                )
                SELECT generation_id,candidate_id,'open',1,updated_at,updated_at
                  FROM mutation_admission_controls LIMIT 1"""
            )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="first-request reservation must initially be reserved at revision 1",
        ):
            raw.execute(
                """INSERT INTO first_request_reservations (
                    reservation_id,plan_id,cutover_run_id,candidate_id,generation_id,
                    request_id,command_name,owner_id,principal_class,run_id,
                    canonical_payload_sha256,state,reservation_revision,reserved_at,consumed_at
                )
                SELECT %s,plan_id,cutover_run_id,candidate_id,generation_id,
                       request_id,command_name,owner_id,principal_class,run_id,
                       canonical_payload_sha256,'cancelled',1,reserved_at,NULL
                  FROM first_request_reservations LIMIT 1""",
                (uuid.uuid4(),),
            )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="release candidate must initially be assembling at revision 1",
        ):
            raw.execute(
                """INSERT INTO release_candidates (
                    candidate_id,generation_id,source_import_batch_id,shadow_baseline_id,
                    projection_epoch_id,source_release,source_commit,ledger_through_commit,
                    schema_head,dish_release,honest_release,protocol_release,openapi_release,
                    routing_release,status,candidate_revision,validation_bundle_sha256,
                    created_at,validated_at,approved_at,terminal_at
                )
                SELECT %s,generation_id,source_import_batch_id,shadow_baseline_id,
                       projection_epoch_id,source_release,source_commit,ledger_through_commit,
                       schema_head,dish_release,honest_release,protocol_release,openapi_release,
                       routing_release,'assembling',2,NULL,
                       created_at,NULL,NULL,NULL
                  FROM release_candidates LIMIT 1""",
                (uuid.uuid4(),),
            )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="mutation admission control must initially be closed at revision 1",
        ):
            raw.execute(
                """INSERT INTO mutation_admission_controls (
                    generation_id,candidate_id,state,control_revision,opened_at,updated_at
                )
                SELECT generation_id,candidate_id,'closed',2,NULL,updated_at
                  FROM mutation_admission_controls LIMIT 1"""
            )
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="first-request reservation must initially be reserved at revision 1",
        ):
            raw.execute(
                """INSERT INTO first_request_reservations (
                    reservation_id,plan_id,cutover_run_id,candidate_id,generation_id,
                    request_id,command_name,owner_id,principal_class,run_id,
                    canonical_payload_sha256,state,reservation_revision,reserved_at,consumed_at
                )
                SELECT %s,plan_id,cutover_run_id,candidate_id,generation_id,
                       request_id,command_name,owner_id,principal_class,run_id,
                       canonical_payload_sha256,'reserved',2,reserved_at,NULL
                  FROM first_request_reservations LIMIT 1""",
                (uuid.uuid4(),),
            )
    finally:
        raw.close()


def test_native_candidate_dependencies_must_match_generation(core_db) -> None:
    factory, ids = core_db
    _seed(factory, ids)
    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    other_generation_id = uuid.uuid4()
    try:
        raw.autocommit = True
        raw.execute(
            """INSERT INTO authority_generations (
                generation_id,predecessor_generation_id,creation_reason,
                external_restore_control_id,schema_head,dish_release,status,
                created_at,retired_at
            )
            SELECT %s,NULL,'initial_cutover',NULL,schema_head,dish_release,'pending',
                   created_at,NULL
              FROM authority_generations LIMIT 1""",
            (other_generation_id,),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            raw.execute(
                """INSERT INTO release_candidates (
                    candidate_id,generation_id,source_import_batch_id,shadow_baseline_id,
                    projection_epoch_id,source_release,source_commit,ledger_through_commit,
                    schema_head,dish_release,honest_release,protocol_release,openapi_release,
                    routing_release,status,candidate_revision,validation_bundle_sha256,
                    created_at,validated_at,approved_at,terminal_at
                )
                SELECT %s,%s,source_import_batch_id,shadow_baseline_id,
                       projection_epoch_id,source_release,source_commit,ledger_through_commit,
                       schema_head,dish_release,honest_release,protocol_release,openapi_release,
                       routing_release,'assembling',1,NULL,created_at,NULL,NULL,NULL
                  FROM release_candidates LIMIT 1""",
                (uuid.uuid4(), other_generation_id),
            )
    finally:
        raw.close()
