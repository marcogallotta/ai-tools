"""Database-boundary coverage for exact first-request reservation consumption."""
from __future__ import annotations

import importlib
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from tests.support.postgresql.core import ROOT, _bootstrap_registry, _import_one, _uuid_stream
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import NOW, _next, _register_run

pytestmark = pytest.mark.pglite


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _upgrade_on(connection, url: str, revision: str) -> None:
    config = _config(url)
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def _seed_open_reservation(session: Session):
    ids = _uuid_stream()
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
            payload={"task_id": str(task.task_id)},
            plan_sha256=HASH_A,
            recorded_at=NOW,
        )
    )
    session.flush()
    payload_sha = "b" * 64
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
            canonical_payload_sha256=payload_sha,
            state="reserved",
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
    session.flush()
    return context, request_id, run_id, payload_sha


def _request(*, request_id, generation_id, run_id, payload_sha, owner="owner-1"):
    return wf.ServiceRequest(
        request_id=request_id,
        generation_id=generation_id,
        run_id=run_id,
        owner_id=owner,
        principal_class="service",
        command_name="start",
        canonical_payload_sha256=payload_sha,
        canonical_payload={"task_id": "fixture"},
        protocol_release="protocol-1",
        dish_release="dish-test",
        admitted_at=NOW,
    )


def _raw_insert_sql() -> str:
    return """INSERT INTO service_requests (
        request_id,generation_id,run_id,owner_id,principal_class,
        command_name,canonical_payload_sha256,canonical_payload,
        protocol_release,dish_release,admitted_at
    ) VALUES (%s,%s,%s,'owner-1','service','start',%s,
              '{"task_id":"fixture"}'::json,'protocol-1','dish-test',%s)"""


def test_0020_different_first_request_is_rejected(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            _upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(
                bind=connection, autoflush=False, expire_on_commit=False
            ) as session:
                with session.begin():
                    context, _request_id, run_id, payload_sha = (
                        _seed_open_reservation(session)
                    )
            raw = connection.connection.driver_connection
            raw.autocommit = True
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="does not match the reserved request",
            ):
                raw.execute(
                    _raw_insert_sql(),
                    (uuid.uuid4(), context["generation_id"], run_id, payload_sha, NOW),
                )
    finally:
        engine.dispose()


def test_0020_exact_request_consumes_and_replay_remains_native(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            _upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(
                bind=connection, autoflush=False, expire_on_commit=False
            ) as session:
                with session.begin():
                    context, request_id, run_id, payload_sha = (
                        _seed_open_reservation(session)
                    )
            raw = connection.connection.driver_connection
            raw.autocommit = True
            params = (
                request_id,
                context["generation_id"],
                run_id,
                payload_sha,
                NOW,
            )
            raw.execute(_raw_insert_sql(), params)
            assert raw.execute(
                """SELECT state,reservation_revision,consumed_at
                     FROM first_request_reservations WHERE request_id=%s""",
                (request_id,),
            ).fetchone() == ("consumed", 2, NOW)
            with pytest.raises(psycopg.errors.UniqueViolation):
                raw.execute(_raw_insert_sql(), params)
    finally:
        engine.dispose()


def test_0020_upgrade_refuses_preexisting_open_admission(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            _upgrade_on(
                connection,
                pglite.sqlalchemy_url,
                "0019_request_run_owner_consistency",
            )
            connection.commit()
            raw = connection.connection.driver_connection
            raw.autocommit = True
            generation_id = uuid.uuid4()
            candidate_id = uuid.uuid4()
            raw.execute("SET session_replication_role = replica")
            raw.execute(
                """INSERT INTO mutation_admission_controls (
                    generation_id, candidate_id, state, control_revision,
                    opened_at, updated_at
                ) VALUES (%s,%s,'open',2,%s,%s)""",
                (generation_id, candidate_id, NOW, NOW),
            )
            raw.execute("SET session_replication_role = origin")
            raw.autocommit = False
            migration = importlib.import_module(
                "dish_pg.migrations.versions.0020_first_request_reservation"
            )
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="mutation admission is open"):
                    migration.upgrade()
    finally:
        engine.dispose()
