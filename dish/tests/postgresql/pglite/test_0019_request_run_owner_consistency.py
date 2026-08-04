"""Database-boundary coverage for exact service-request run ownership."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from sqlalchemy import create_engine

import importlib

from tests.support.postgresql.core import ROOT

pytestmark = pytest.mark.pglite


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _upgrade_on(connection, url: str, revision: str) -> None:
    config = _config(url)
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def _insert_generation(connection, generation_id: uuid.UUID) -> None:
    connection.execute(
        """
        INSERT INTO authority_generations (
            generation_id, predecessor_generation_id, creation_reason,
            external_restore_control_id, schema_head, dish_release,
            status, created_at, retired_at
        ) VALUES (%s, NULL, 'initial_cutover', NULL, %s, 'test-release',
                  'active', %s, NULL)
        """,
        (generation_id, "0018_projection_attempt_lifecycle", datetime.now(timezone.utc)),
    )


def _insert_run(
    connection,
    *,
    generation_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_id: str,
    digest_byte: bytes,
) -> None:
    connection.execute(
        """
        INSERT INTO service_runs (
            run_id, generation_id, owner_id, agent, capability_digest,
            bootstrap_id, status, registered_at, retired_at
        ) VALUES (%s, %s, %s, 'service', %s, NULL, 'active', %s, NULL)
        """,
        (run_id, generation_id, owner_id, digest_byte * 32, datetime.now(timezone.utc)),
    )


def _insert_request(
    connection,
    *,
    generation_id: uuid.UUID,
    request_id: uuid.UUID,
    run_id: uuid.UUID,
    owner_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO service_requests (
            request_id, generation_id, run_id, owner_id, principal_class,
            command_name, canonical_payload_sha256, canonical_payload,
            protocol_release, dish_release, admitted_at
        ) VALUES (%s, %s, %s, %s, 'service', 'inspect', %s, '{}'::json,
                  'protocol-test', 'dish-test', %s)
        """,
        (
            request_id,
            generation_id,
            run_id,
            owner_id,
            "a" * 64,
            datetime.now(timezone.utc),
        ),
    )


def test_0019_rejects_request_owned_by_someone_other_than_run_owner(pglite) -> None:
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
            run_id = uuid.uuid4()
            _insert_generation(raw, generation_id)
            _insert_run(
                raw,
                generation_id=generation_id,
                run_id=run_id,
                owner_id="owner-a",
                digest_byte=b"a",
            )
            _insert_request(
                raw,
                generation_id=generation_id,
                request_id=uuid.uuid4(),
                run_id=run_id,
                owner_id="owner-a",
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                _insert_request(
                    raw,
                    generation_id=generation_id,
                    request_id=uuid.uuid4(),
                    run_id=run_id,
                    owner_id="owner-b",
                )
    finally:
        engine.dispose()


def test_0019_upgrade_refuses_mismatched_predecessor_rows(pglite) -> None:
    generation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            _upgrade_on(
                connection, pglite.sqlalchemy_url, "0018_projection_attempt_lifecycle"
            )
            connection.commit()
            raw = connection.connection.driver_connection
            _insert_generation(raw, generation_id)
            _insert_run(
                raw,
                generation_id=generation_id,
                run_id=run_id,
                owner_id="owner-a",
                digest_byte=b"b",
            )
            _insert_request(
                raw,
                generation_id=generation_id,
                request_id=uuid.uuid4(),
                run_id=run_id,
                owner_id="owner-b",
            )
            migration = importlib.import_module(
                "dish_pg.migrations.versions.0019_request_run_owner_consistency"
            )
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="predecessor service request row"):
                    migration.upgrade()
    finally:
        engine.dispose()
