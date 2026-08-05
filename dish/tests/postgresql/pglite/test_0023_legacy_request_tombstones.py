"""PostgreSQL boundary tests for immutable legacy request identities."""
from __future__ import annotations

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dish_pg import legacy_request_models as legacy
from tests.support.postgresql.core import ROOT
from tests.support.postgresql.workflow import NOW

from tests.support.postgresql.pglite_fixtures import seed_open_reservation, upgrade_on

pytestmark = pytest.mark.pglite




def _seed_tombstone(session: Session):
    context, request_id, run_id, payload_sha = seed_open_reservation(session)
    session.add(legacy.LegacyRequestTombstone(
        tombstone_id=request_id, request_id=request_id,
        source_authority="legacy-sqlite", import_run_id=context["import_run_id"],
        import_batch_id=None, source_identity_sha256="a" * 64,
        source_metadata={"source": "requests"}, imported_at=NOW,
    ))
    session.flush()
    return context, request_id, run_id, payload_sha


def test_0023_tombstoned_legacy_request_is_rejected_before_native_admission(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    context, request_id, run_id, payload_sha = _seed_tombstone(session)
            raw = connection.connection.driver_connection
            raw.autocommit = True
            with pytest.raises(psycopg.errors.RaiseException, match="reserved by legacy authority"):
                raw.execute(
                    """INSERT INTO service_requests
                    (request_id,generation_id,run_id,owner_id,principal_class,command_name,
                     canonical_payload_sha256,canonical_payload,protocol_release,dish_release,admitted_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::json,%s,%s,%s)""",
                    (request_id, context["generation_id"], run_id, "owner-1", "service",
                     "start", payload_sha, '{"task_id":"fixture"}',
                     "protocol-1", "dish-test", NOW),
                )
    finally:
        engine.dispose()


def test_0023_tombstones_are_immutable(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    _context, request_id, _run_id, _payload_sha = _seed_tombstone(session)
            raw = connection.connection.driver_connection
            raw.autocommit = True
            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                raw.execute(
                    "UPDATE legacy_request_tombstones SET source_authority=%s WHERE request_id=%s",
                    ("changed", request_id),
                )
    finally:
        engine.dispose()
