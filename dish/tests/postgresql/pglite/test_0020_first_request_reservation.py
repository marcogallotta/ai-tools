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

from tests.support.postgresql.pglite_fixtures import seed_open_reservation, upgrade_on

pytestmark = pytest.mark.pglite


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
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(
                bind=connection, autoflush=False, expire_on_commit=False
            ) as session:
                with session.begin():
                    context, _request_id, run_id, payload_sha = (
                        seed_open_reservation(session)
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


def test_0020_exact_request_consumes_and_replay_waits_for_verification(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(
                bind=connection, autoflush=False, expire_on_commit=False
            ) as session:
                with session.begin():
                    context, request_id, run_id, payload_sha = (
                        seed_open_reservation(session)
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
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="pending first-admission verification",
            ):
                raw.execute(_raw_insert_sql(), params)
    finally:
        engine.dispose()


def test_0020_upgrade_refuses_preexisting_open_admission(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(
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
