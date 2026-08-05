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

from tests.support.postgresql.pglite_fixtures import alembic_config

pytestmark = pytest.mark.pglite
ROOT = Path(__file__).resolve().parents[3]




def test_pglite_upgrades_empty_database_through_head(pglite) -> None:
    command.upgrade(alembic_config(pglite.sqlalchemy_url), "head")
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
    command.upgrade(alembic_config(pglite.sqlalchemy_url), "head")
    with psycopg.connect(pglite.libpq_dsn) as first:
        assert first.execute(
            "SELECT to_regclass('public.authority_generations')"
        ).fetchone()[0] == "authority_generations"
    with psycopg.connect(pglite.libpq_dsn) as second:
        assert second.execute(
            "SELECT to_regclass('public.service_requests')"
        ).fetchone()[0] == "service_requests"


def test_pglite_accepts_service_run_for_active_generation(pglite) -> None:
    command.upgrade(alembic_config(pglite.sqlalchemy_url), "head")
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


@pytest.mark.quarantined(
    issue="DISH-STAGE-A-PGLITE",
    owner="Marco",
    first_seen="2026-08-02",
    quarantined_on="2026-08-02",
    expires="2026-08-09",
    signature="server closed the connection unexpectedly during PGlite TCP startup under full-suite load",
)
def test_native_fixture_reset_uses_alembic_history(pglite) -> None:
    from tests.support.postgresql.core import _reset_postgresql_schema

    _reset_postgresql_schema(pglite.sqlalchemy_url)
    with psycopg.connect(pglite.libpq_dsn) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == ALEMBIC_HEAD
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
        ).fetchone()[0] > 0


def test_pglite_rejects_duplicate_task_level_grant(pglite) -> None:
    command.upgrade(alembic_config(pglite.sqlalchemy_url), "head")
    with psycopg.connect(pglite.libpq_dsn) as connection:
        index = connection.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'uq_marco_grant_task_semantic_identity'
            """
        ).fetchone()
    assert index is not None
    assert "UNIQUE INDEX" in index[0]
    assert "WHERE (operation_id IS NULL)" in index[0]


def _install_honest_binding_predecessor(pglite) -> Config:
    config = alembic_config(pglite.sqlalchemy_url)
    with psycopg.connect(pglite.libpq_dsn) as connection:
        connection.execute(
            """
            CREATE TABLE honest_contract_bindings (
                binding_id UUID PRIMARY KEY,
                binding_kind TEXT NOT NULL,
                source_identity TEXT NOT NULL,
                dish_release TEXT NOT NULL,
                honest_release TEXT NOT NULL,
                protocol_release TEXT NOT NULL,
                protocol_sha256 CHAR(64) NOT NULL,
                schema_release TEXT NOT NULL,
                schema_sha256 CHAR(64) NOT NULL,
                migration_id TEXT NULL,
                source_schema_version TEXT NULL,
                target_schema_version TEXT NULL,
                migration_metadata_sha256 CHAR(64) NULL,
                source_ids JSON NOT NULL,
                provenance JSON NOT NULL,
                resolved_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT ck_honest_binding_kind_payload_consistent CHECK (
                    (binding_kind IN ('release','task_schema')
                     AND migration_id IS NULL
                     AND source_schema_version IS NULL
                     AND target_schema_version IS NULL
                     AND migration_metadata_sha256 IS NULL)
                    OR
                    (binding_kind = 'migration'
                     AND migration_id IS NOT NULL
                     AND source_schema_version IS NOT NULL
                     AND target_schema_version IS NOT NULL
                     AND migration_metadata_sha256 IS NOT NULL)
                ),
                CONSTRAINT uq_honest_binding_exact_identity UNIQUE (
                    binding_kind,
                    protocol_sha256,
                    schema_sha256,
                    migration_id,
                    migration_metadata_sha256
                )
            )
            """
        )
        connection.commit()
    command.stamp(config, "0015_verification_cycle_sequence")
    return config


def _insert_honest_binding(
    connection,
    *,
    binding_id: uuid.UUID,
    source_identity: str,
    protocol_sha256: str,
    schema_sha256: str,
    migration_id: str | None = None,
    migration_metadata_sha256: str | None = None,
) -> None:
    binding_kind = "migration" if migration_id is not None else "release"
    source_schema_version = "v1" if migration_id is not None else None
    target_schema_version = "v2" if migration_id is not None else None
    connection.execute(
        """
        INSERT INTO honest_contract_bindings (
            binding_id, binding_kind, source_identity, dish_release,
            honest_release, protocol_release, protocol_sha256,
            schema_release, schema_sha256, migration_id,
            source_schema_version, target_schema_version,
            migration_metadata_sha256, source_ids, provenance, resolved_at
        ) VALUES (
            %s, %s, %s, 'dish-test', 'honest-test', 'protocol-test', %s,
            'schema-test', %s, %s, %s, %s, %s, '{}'::json, '{}'::json, %s
        )
        """,
        (
            binding_id,
            binding_kind,
            source_identity,
            protocol_sha256,
            schema_sha256,
            migration_id,
            source_schema_version,
            target_schema_version,
            migration_metadata_sha256,
            datetime.now(timezone.utc),
        ),
    )


def test_pglite_honest_binding_upgrade_enforces_null_safe_exact_identity(pglite) -> None:
    config = _install_honest_binding_predecessor(pglite)
    protocol_hash = "1" * 64
    schema_hash = "2" * 64
    with psycopg.connect(pglite.libpq_dsn) as connection:
        _insert_honest_binding(
            connection,
            binding_id=uuid.uuid4(),
            source_identity="valid-predecessor-release",
            protocol_sha256=protocol_hash,
            schema_sha256=schema_hash,
        )
        _insert_honest_binding(
            connection,
            binding_id=uuid.uuid4(),
            source_identity="valid-predecessor-migration",
            protocol_sha256=protocol_hash,
            schema_sha256=schema_hash,
            migration_id="migration-v1-v2",
            migration_metadata_sha256="3" * 64,
        )
        connection.commit()

    command.upgrade(config, "0016_honest_binding_null_identity")

    # Reuse one post-upgrade connection because PGlite's TCP shim can become
    # unavailable between rapid reconnects. The expected violation remains the
    # final database action because it can invalidate the current connection.
    connection = psycopg.connect(pglite.libpq_dsn)
    try:
        definition = connection.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'uq_honest_binding_null_identity'
            """
        ).fetchone()[0]
        assert "UNIQUE INDEX" in definition
        assert "migration_id IS NULL" in definition
        assert "migration_metadata_sha256 IS NULL" in definition
        _insert_honest_binding(
            connection,
            binding_id=uuid.uuid4(),
            source_identity="genuinely-distinct-protocol",
            protocol_sha256="4" * 64,
            schema_sha256=schema_hash,
        )
        _insert_honest_binding(
            connection,
            binding_id=uuid.uuid4(),
            source_identity="genuinely-distinct-migration",
            protocol_sha256=protocol_hash,
            schema_sha256=schema_hash,
            migration_id="migration-v2-v3",
            migration_metadata_sha256="5" * 64,
        )
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM honest_contract_bindings"
        ).fetchone()[0] == 4
        connection.commit()
        connection.autocommit = True
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_honest_binding(
                connection,
                binding_id=uuid.uuid4(),
                source_identity="same-logical-identity-different-source",
                protocol_sha256=protocol_hash,
                schema_sha256=schema_hash,
            )
    finally:
        connection.close()


def test_pglite_honest_binding_upgrade_rejects_conflicting_predecessor_data(pglite) -> None:
    config = _install_honest_binding_predecessor(pglite)
    with psycopg.connect(pglite.libpq_dsn) as connection:
        for source_identity in ("predecessor-duplicate-a", "predecessor-duplicate-b"):
            _insert_honest_binding(
                connection,
                binding_id=uuid.uuid4(),
                source_identity=source_identity,
                protocol_sha256="6" * 64,
                schema_sha256="7" * 64,
            )
        connection.commit()

    with pytest.raises(
        RuntimeError,
        match="predecessor data contains duplicate exact identity",
    ):
        command.upgrade(config, "0016_honest_binding_null_identity")
