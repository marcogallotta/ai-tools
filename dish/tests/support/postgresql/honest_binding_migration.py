"""Reusable 0015 -> 0016 populated-predecessor seeds and assertions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests.support.postgresql.migrations import MigrationDatabase

PREDECESSOR_REVISION = "0015_verification_cycle_sequence"
TARGET_REVISION = "0016_honest_binding_null_identity"
_DUPLICATE_MESSAGE = "predecessor data contains duplicate exact identity"
NOW = datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc)

metadata = sa.MetaData()
honest_contract_bindings = sa.Table(
    "honest_contract_bindings",
    metadata,
    sa.Column("binding_id", sa.Uuid(), primary_key=True),
    sa.Column("binding_kind", sa.String(), nullable=False),
    sa.Column("source_identity", sa.String(), nullable=False),
    sa.Column("dish_release", sa.String(), nullable=False),
    sa.Column("honest_release", sa.String(), nullable=False),
    sa.Column("protocol_release", sa.String(), nullable=False),
    sa.Column("protocol_sha256", sa.String(64), nullable=False),
    sa.Column("schema_release", sa.String(), nullable=False),
    sa.Column("schema_sha256", sa.String(64), nullable=False),
    sa.Column("migration_id", sa.String(), nullable=True),
    sa.Column("source_schema_version", sa.String(), nullable=True),
    sa.Column("target_schema_version", sa.String(), nullable=True),
    sa.Column("migration_metadata_sha256", sa.String(64), nullable=True),
    sa.Column("source_ids", sa.JSON(), nullable=False),
    sa.Column("provenance", sa.JSON(), nullable=False),
    sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "binding_kind IN ('release','task_schema','migration')",
        name="ck_honest_contract_bindings_binding_kind_allowed",
    ),
    sa.CheckConstraint(
        "length(protocol_sha256) = 64",
        name="ck_honest_contract_bindings_protocol_hash_length",
    ),
    sa.CheckConstraint(
        "length(schema_sha256) = 64",
        name="ck_honest_contract_bindings_schema_hash_length",
    ),
    sa.CheckConstraint(
        "(binding_kind <> 'migration' AND migration_id IS NULL "
        "AND source_schema_version IS NULL AND target_schema_version IS NULL "
        "AND migration_metadata_sha256 IS NULL) OR "
        "(binding_kind = 'migration' AND migration_id IS NOT NULL "
        "AND source_schema_version IS NOT NULL AND target_schema_version IS NOT NULL "
        "AND length(migration_metadata_sha256) = 64)",
        name="ck_honest_contract_bindings_migration_fields_match_kind",
    ),
    sa.UniqueConstraint(
        "binding_kind",
        "protocol_sha256",
        "schema_sha256",
        "migration_id",
        "migration_metadata_sha256",
        name="uq_honest_binding_exact_identity",
    ),
)


def install_predecessor(database: MigrationDatabase) -> None:
    database.reset()
    engine = database.create_engine()
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    database.stamp(PREDECESSOR_REVISION)


def _row(
    *,
    source_identity: str,
    protocol_sha256: str,
    schema_sha256: str,
    migration_id: str | None = None,
    migration_metadata_sha256: str | None = None,
) -> dict[str, object]:
    migration = migration_id is not None
    return {
        "binding_id": uuid.uuid4(),
        "binding_kind": "migration" if migration else "release",
        "source_identity": source_identity,
        "dish_release": "dish-test",
        "honest_release": "honest-test",
        "protocol_release": "protocol-test",
        "protocol_sha256": protocol_sha256,
        "schema_release": "schema-test",
        "schema_sha256": schema_sha256,
        "migration_id": migration_id,
        "source_schema_version": "v1" if migration else None,
        "target_schema_version": "v2" if migration else None,
        "migration_metadata_sha256": migration_metadata_sha256,
        "source_ids": {},
        "provenance": {},
        "resolved_at": NOW,
    }


def seed_valid_predecessor(database: MigrationDatabase) -> None:
    rows = [
        _row(
            source_identity="valid-release",
            protocol_sha256="1" * 64,
            schema_sha256="2" * 64,
        ),
        _row(
            source_identity="valid-migration",
            protocol_sha256="1" * 64,
            schema_sha256="2" * 64,
            migration_id="migration-v1-v2",
            migration_metadata_sha256="3" * 64,
        ),
    ]
    database.seed(lambda connection: connection.execute(honest_contract_bindings.insert(), rows))


def seed_conflicting_predecessor(database: MigrationDatabase) -> None:
    rows = [
        _row(
            source_identity=source_identity,
            protocol_sha256="6" * 64,
            schema_sha256="7" * 64,
        )
        for source_identity in ("duplicate-a", "duplicate-b")
    ]
    database.seed(lambda connection: connection.execute(honest_contract_bindings.insert(), rows))


def assert_null_safe_identity_enforced(database: MigrationDatabase) -> None:
    duplicate = _row(
        source_identity="same-logical-identity-different-source",
        protocol_sha256="1" * 64,
        schema_sha256="2" * 64,
    )
    try:
        database.seed(
            lambda connection: connection.execute(
                honest_contract_bindings.insert(), duplicate
            )
        )
    except IntegrityError:
        return
    raise AssertionError("post-upgrade partial uniqueness accepted a duplicate null identity")


def assert_conflicting_upgrade_rejected(database: MigrationDatabase) -> None:
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=_DUPLICATE_MESSAGE,
    )
