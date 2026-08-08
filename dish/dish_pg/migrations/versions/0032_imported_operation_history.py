"""Import terminal legacy operation history without fabricating live authority.

Revision ID: 0032_imported_operation_history
Revises: 0031_worker_readiness_consolidation
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_imported_operation_history"
down_revision = "0031_worker_readiness_consolidation"
branch_labels = None
depends_on = None

_WORKFLOW_PROVENANCE = "ck_workflow_operations_creation_provenance_exact"
_LEASE_PROVENANCE = "ck_service_leases_provenance_exact"
_LEASE_CLASSIFICATION = "ck_service_leases_classification_context_complete"
_CYCLE_PROVENANCE = "ck_verification_cycles_creation_provenance_exact"

_PROVENANCE_COLUMNS = {
    "workflow_operations": ("import_run_id", "creation_request_id", "creation_execution_id"),
    "service_leases": ("import_run_id", "run_id", "source_run_id"),
    "verification_cycles": ("import_run_id", "reviewed_content_version_id", "created_by_execution_id"),
}

_LIVE_LEASE_CLASSIFICATION = (
    "(lease_kind = 'actor' AND operation_id IS NOT NULL AND actor_role IS NOT NULL "
    "AND actor_attempt_sequence IS NOT NULL) OR "
    "(lease_kind = 'admin_request' AND actor_role IS NULL "
    "AND actor_attempt_sequence IS NULL AND verification_cycle_id IS NULL)"
)
_IMPORT_LEASE_CLASSIFICATION = (
    "(lease_kind = 'actor' AND operation_id IS NOT NULL AND actor_attempt_sequence IS NOT NULL "
    "AND (import_run_id IS NOT NULL OR actor_role IS NOT NULL)) OR "
    "(lease_kind = 'admin_request' AND actor_role IS NULL "
    "AND actor_attempt_sequence IS NULL AND verification_cycle_id IS NULL)"
)


def _install_provenance_immutability() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_reject_import_provenance_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'import provenance is immutable';
            END;
            $$
            """
        )
        for table, columns in _PROVENANCE_COLUMNS.items():
            op.execute(
                f"CREATE TRIGGER {table}_import_provenance_immutable "
                f"BEFORE UPDATE OF {','.join(columns)} ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION dish_reject_import_provenance_mutation()"
            )
    elif bind.dialect.name == "sqlite":
        for table, columns in _PROVENANCE_COLUMNS.items():
            op.execute(
                f"CREATE TRIGGER {table}_import_provenance_immutable "
                f"BEFORE UPDATE OF {','.join(columns)} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'import provenance is immutable'); END"
            )


def _drop_provenance_immutability() -> None:
    bind = op.get_bind()
    for table in _PROVENANCE_COLUMNS:
        if bind.dialect.name == "postgresql":
            op.execute(f"DROP TRIGGER IF EXISTS {table}_import_provenance_immutable ON {table}")
        elif bind.dialect.name == "sqlite":
            op.execute(f"DROP TRIGGER IF EXISTS {table}_import_provenance_immutable")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS dish_reject_import_provenance_mutation()")


def upgrade() -> None:
    with op.batch_alter_table("workflow_operations") as batch:
        batch.add_column(sa.Column("import_run_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_workflow_operations_import_run_id_stage_a_import_runs"),
            "stage_a_import_runs", ["import_run_id"], ["import_run_id"], ondelete="RESTRICT",
        )
        batch.alter_column("creation_request_id", existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("creation_execution_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(
            op.f(_WORKFLOW_PROVENANCE),
            "(import_run_id IS NULL AND creation_request_id IS NOT NULL "
            "AND creation_execution_id IS NOT NULL) OR "
            "(import_run_id IS NOT NULL AND creation_request_id IS NULL "
            "AND creation_execution_id IS NULL)",
        )

    with op.batch_alter_table("service_leases") as batch:
        batch.drop_constraint(op.f(_LEASE_CLASSIFICATION), type_="check")
        batch.add_column(sa.Column("import_run_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("source_run_id", sa.String(256), nullable=True))
        batch.create_foreign_key(
            op.f("fk_service_leases_import_run_id_stage_a_import_runs"),
            "stage_a_import_runs", ["import_run_id"], ["import_run_id"], ondelete="RESTRICT",
        )
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(
            op.f(_LEASE_PROVENANCE),
            "(import_run_id IS NULL AND run_id IS NOT NULL AND source_run_id IS NULL) OR "
            "(import_run_id IS NOT NULL AND run_id IS NULL AND length(trim(source_run_id)) > 0)",
        )
        batch.create_check_constraint(op.f(_LEASE_CLASSIFICATION), _IMPORT_LEASE_CLASSIFICATION)

    with op.batch_alter_table("verification_cycles") as batch:
        batch.add_column(sa.Column("import_run_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_verification_cycles_import_run_id_stage_a_import_runs"),
            "stage_a_import_runs", ["import_run_id"], ["import_run_id"], ondelete="RESTRICT",
        )
        batch.alter_column("reviewed_content_version_id", existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("created_by_execution_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(
            op.f(_CYCLE_PROVENANCE),
            "(import_run_id IS NULL AND reviewed_content_version_id IS NOT NULL "
            "AND created_by_execution_id IS NOT NULL) OR "
            "(import_run_id IS NOT NULL AND reviewed_content_version_id IS NULL "
            "AND created_by_execution_id IS NULL)",
        )

    _install_provenance_immutability()


def downgrade() -> None:
    bind = op.get_bind()
    for table in _PROVENANCE_COLUMNS:
        if int(bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE import_run_id IS NOT NULL")).scalar_one()):
            raise RuntimeError("refusing lossy downgrade: imported operation history exists")

    _drop_provenance_immutability()

    with op.batch_alter_table("verification_cycles") as batch:
        batch.drop_constraint(op.f(_CYCLE_PROVENANCE), type_="check")
        batch.alter_column("reviewed_content_version_id", existing_type=sa.Uuid(), nullable=False)
        batch.alter_column("created_by_execution_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_constraint(op.f("fk_verification_cycles_import_run_id_stage_a_import_runs"), type_="foreignkey")
        batch.drop_column("import_run_id")

    with op.batch_alter_table("service_leases") as batch:
        batch.drop_constraint(op.f(_LEASE_CLASSIFICATION), type_="check")
        batch.drop_constraint(op.f(_LEASE_PROVENANCE), type_="check")
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_check_constraint(op.f(_LEASE_CLASSIFICATION), _LIVE_LEASE_CLASSIFICATION)
        batch.drop_constraint(op.f("fk_service_leases_import_run_id_stage_a_import_runs"), type_="foreignkey")
        batch.drop_column("source_run_id")
        batch.drop_column("import_run_id")

    with op.batch_alter_table("workflow_operations") as batch:
        batch.drop_constraint(op.f(_WORKFLOW_PROVENANCE), type_="check")
        batch.alter_column("creation_request_id", existing_type=sa.Uuid(), nullable=False)
        batch.alter_column("creation_execution_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_constraint(op.f("fk_workflow_operations_import_run_id_stage_a_import_runs"), type_="foreignkey")
        batch.drop_column("import_run_id")
