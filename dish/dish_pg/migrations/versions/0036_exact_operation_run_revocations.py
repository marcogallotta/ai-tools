"""Persist exact operation/owner/run revocation independently of lease/run state.

Revision ID: 0036_exact_operation_run_revocations
Revises: 0035_persistence_constraint_integrity
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_exact_operation_run_revocations"
down_revision = "0035_persistence_constraint_integrity"
branch_labels = None
depends_on = None

_OPERATION_GENERATION_UQ = "uq_workflow_operations_generation_operation"


def _install_immutability() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER operation_run_revocations_immutable_update "
            "BEFORE UPDATE ON operation_run_revocations FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_workflow_authority()"
        )
        op.execute(
            "CREATE TRIGGER operation_run_revocations_immutable_delete "
            "BEFORE DELETE ON operation_run_revocations FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_workflow_authority()"
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER operation_run_revocations_immutable_update "
            "BEFORE UPDATE ON operation_run_revocations "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )
        op.execute(
            "CREATE TRIGGER operation_run_revocations_immutable_delete "
            "BEFORE DELETE ON operation_run_revocations "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )


def upgrade() -> None:
    # Pre-0036 imports intentionally are not synthesized here: the migration has no
    # authoritative legacy SQLite source from which to recover omitted explicit
    # revocations. Runtime authority therefore treats legacy-imported operations
    # without revocation-aware import provenance as unreconciled and fails closed
    # until a v2 supplemental legacy snapshot explicitly reconciles revocations
    # (including an attested empty set). Lease release/expiry is never used as a
    # substitute source.
    #
    # operation_id is already globally unique. This redundant composite unique
    # key lets the revocation row carry generation identity in its FK so a
    # malformed cross-generation tuple is rejected by the database itself.
    with op.batch_alter_table("workflow_operations") as batch:
        batch.create_unique_constraint(
            _OPERATION_GENERATION_UQ, ["generation_id", "operation_id"]
        )

    op.create_table(
        "operation_run_revocations",
        sa.Column("revocation_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=256), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("import_run_id", sa.Uuid(), nullable=True),
        sa.Column("source_run_id", sa.String(length=256), nullable=True),
        sa.Column("source_lease_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(owner_id)) > 0", name=op.f("ck_operation_run_revocations_owner_nonblank")),
        sa.CheckConstraint("length(trim(reason)) > 0", name=op.f("ck_operation_run_revocations_reason_nonblank")),
        sa.CheckConstraint(
            "(import_run_id IS NULL AND run_id IS NOT NULL AND source_run_id IS NULL) OR "
            "(import_run_id IS NOT NULL AND run_id IS NULL AND source_run_id IS NOT NULL "
            "AND length(trim(source_run_id)) > 0)",
            name=op.f("ck_operation_run_revocations_provenance_exact"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "operation_id"],
            ["workflow_operations.generation_id", "workflow_operations.operation_id"],
            name=op.f("fk_operation_run_revocations_generation_operation_workflow_operations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["service_runs.run_id"],
            name=op.f("fk_operation_run_revocations_run_id_service_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"], ["stage_a_import_runs.import_run_id"],
            name=op.f("fk_operation_run_revocations_import_run_id_stage_a_import_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_lease_id"], ["service_leases.lease_id"],
            name=op.f("fk_operation_run_revocations_source_lease_id_service_leases"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("revocation_id", name=op.f("pk_operation_run_revocations")),
    )
    op.create_index(
        "uq_operation_run_revocations_live_exact",
        "operation_run_revocations",
        ["generation_id", "operation_id", "owner_id", "run_id"],
        unique=True,
        postgresql_where=sa.text("import_run_id IS NULL"),
        sqlite_where=sa.text("import_run_id IS NULL"),
    )
    op.create_index(
        "uq_operation_run_revocations_imported_exact",
        "operation_run_revocations",
        ["generation_id", "operation_id", "owner_id", "source_run_id"],
        unique=True,
        postgresql_where=sa.text("import_run_id IS NOT NULL"),
        sqlite_where=sa.text("import_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_operation_run_revocations_operation_time",
        "operation_run_revocations",
        ["generation_id", "operation_id", "revoked_at"],
        unique=False,
    )
    _install_immutability()


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql:
        count = int(bind.execute(sa.text("SELECT count(*) FROM operation_run_revocations")).scalar_one())
        if count:
            raise RuntimeError(
                "refusing lossy downgrade: exact operation/run revocations exist"
            )
    op.drop_table("operation_run_revocations")
    with op.batch_alter_table("workflow_operations") as batch:
        batch.drop_constraint(_OPERATION_GENERATION_UQ, type_="unique")
