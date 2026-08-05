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

from tests.support.postgresql.pglite_fixtures import insert_generation, insert_request, insert_run, upgrade_on

pytestmark = pytest.mark.pglite












def test_0019_rejects_request_owned_by_someone_other_than_run_owner(pglite) -> None:
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
            run_id = uuid.uuid4()
            insert_generation(raw, generation_id)
            insert_run(
                raw,
                generation_id=generation_id,
                run_id=run_id,
                owner_id="owner-a",
                digest_byte=b"a",
            )
            insert_request(
                raw,
                generation_id=generation_id,
                request_id=uuid.uuid4(),
                run_id=run_id,
                owner_id="owner-a",
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                insert_request(
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
            upgrade_on(
                connection, pglite.sqlalchemy_url, "0018_projection_attempt_lifecycle"
            )
            connection.commit()
            raw = connection.connection.driver_connection
            insert_generation(raw, generation_id)
            insert_run(
                raw,
                generation_id=generation_id,
                run_id=run_id,
                owner_id="owner-a",
                digest_byte=b"b",
            )
            insert_request(
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
