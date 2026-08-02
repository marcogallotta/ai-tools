"""PostgreSQL-semantic migration checks runnable on PGlite.

These tests catch PostgreSQL DDL and transaction-ownership errors hidden by SQLite
compatibility. They are not native PostgreSQL certification evidence.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from dish_pg.release import ALEMBIC_HEAD

pytestmark = pytest.mark.pglite
ROOT = Path(__file__).resolve().parents[3]


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_pglite_upgrades_empty_database_through_head(pglite) -> None:
    command.upgrade(_config(pglite.sqlalchemy_url), "head")
    with psycopg.connect(pglite.libpq_dsn) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        trigger_count = connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
        ).fetchone()[0]
    assert version == ALEMBIC_HEAD
    assert trigger_count > 0


def test_pglite_persists_migrated_schema_across_connections(pglite) -> None:
    command.upgrade(_config(pglite.sqlalchemy_url), "head")
    with psycopg.connect(pglite.libpq_dsn) as first:
        assert first.execute(
            "SELECT to_regclass('public.authority_generations')"
        ).fetchone()[0] == "authority_generations"
    with psycopg.connect(pglite.libpq_dsn) as second:
        assert second.execute(
            "SELECT to_regclass('public.service_requests')"
        ).fetchone()[0] == "service_requests"


def test_pglite_accepts_service_run_for_active_generation(pglite) -> None:
    command.upgrade(_config(pglite.sqlalchemy_url), "head")
    generation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with psycopg.connect(pglite.libpq_dsn) as connection:
        connection.execute(
            """
            INSERT INTO authority_generations (
                generation_id, predecessor_generation_id, creation_reason,
                external_restore_control_id, schema_head, dish_release,
                status, created_at, retired_at
            ) VALUES (%s, NULL, 'initial_cutover', NULL, %s, 'test-release',
                      'active', %s, NULL)
            """,
            (generation_id, ALEMBIC_HEAD, now),
        )
        connection.execute(
            """
            INSERT INTO service_runs (
                run_id, generation_id, owner_id, agent, capability_digest,
                bootstrap_id, status, registered_at, retired_at
            ) VALUES (%s, %s, 'owner', 'service', %s, NULL, 'active', %s, NULL)
            """,
            (run_id, generation_id, b"x" * 32, now),
        )
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM service_runs WHERE run_id = %s", (run_id,)
        ).fetchone()[0] == 1
