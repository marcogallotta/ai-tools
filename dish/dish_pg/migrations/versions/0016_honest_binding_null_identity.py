"""Enforce null-safe exact identity for honest contract bindings.

Revision ID: 0016_honest_binding_null_identity
Revises: 0015_verification_cycle_sequence
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0016_honest_binding_null_identity"
down_revision = "0015_verification_cycle_sequence"
branch_labels = None
depends_on = None

_TABLE = "honest_contract_bindings"
_NULL_IDENTITY_INDEX = "uq_honest_binding_null_identity"


def _reject_predecessor_duplicates() -> None:
    if context.is_offline_mode():
        return
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT
                binding_kind,
                protocol_sha256,
                schema_sha256,
                migration_id,
                migration_metadata_sha256,
                count(*) AS duplicate_count
            FROM honest_contract_bindings
            GROUP BY
                binding_kind,
                protocol_sha256,
                schema_sha256,
                migration_id,
                migration_metadata_sha256
            HAVING count(*) > 1
            ORDER BY binding_kind, protocol_sha256, schema_sha256
            LIMIT 1
            """
        )
    ).mappings().first()
    if duplicate is None:
        return
    raise RuntimeError(
        "cannot install null-safe honest binding uniqueness: predecessor data "
        "contains duplicate exact identity "
        f"kind={duplicate['binding_kind']!r}, "
        f"protocol_sha256={duplicate['protocol_sha256']!r}, "
        f"schema_sha256={duplicate['schema_sha256']!r}, "
        f"migration_id={duplicate['migration_id']!r}, "
        f"migration_metadata_sha256={duplicate['migration_metadata_sha256']!r}, "
        f"count={duplicate['duplicate_count']}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    _reject_predecessor_duplicates()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError(
            f"unsupported database dialect for null-safe honest binding uniqueness: {bind.dialect.name}"
        )
    # The binding-kind check guarantees that the two optional exact-identity
    # fields are both null for release/task_schema rows and both non-null for
    # migration rows. The existing unique constraint already covers the latter;
    # this partial unique index closes the PostgreSQL null-distinct hole for the
    # former without rewriting the applied predecessor migration.
    op.create_index(
        _NULL_IDENTITY_INDEX,
        _TABLE,
        ("binding_kind", "protocol_sha256", "schema_sha256"),
        unique=True,
        postgresql_where=sa.text(
            "migration_id IS NULL AND migration_metadata_sha256 IS NULL"
        ),
        sqlite_where=sa.text(
            "migration_id IS NULL AND migration_metadata_sha256 IS NULL"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError(
            f"unsupported database dialect for null-safe honest binding uniqueness: {bind.dialect.name}"
        )
    op.drop_index(_NULL_IDENTITY_INDEX, table_name=_TABLE)
