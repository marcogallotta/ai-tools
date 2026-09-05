"""Add the inert native runtime-authority schema spine.

This migration is schema-only. It does not create a runtime attestation or
current pointer and therefore cannot switch runtime authority.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0049_native_catalog_runtime_authority_root"
down_revision = "0048_native_section_content_carry_forward"
branch_labels = None
depends_on = None

def _suspend_sqlite_triggers(table: str) -> tuple[str, ...]:
    if context.is_offline_mode() or op.get_bind().dialect.name != "sqlite":
        return ()
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE ?",
        (f"%{table}%",),
    ).all()
    for name, _sql in rows:
        connection.exec_driver_sql(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
    return tuple(sql for _name, sql in rows if sql)


def _restore_sqlite_triggers(statements: tuple[str, ...]) -> None:
    for statement in statements:
        op.get_bind().exec_driver_sql(statement)


def _add_shared_columns() -> None:
    offline = context.is_offline_mode()

    if offline:
        op.add_column("dish_states", sa.Column("catalog_version_id", sa.Uuid()))
        op.create_foreign_key(
            "fk_dish_states_catalog_version",
            "dish_states",
            "section_catalog_versions",
            ["catalog_version_id"],
            ["catalog_version_id"],
            ondelete="RESTRICT",
        )

        op.add_column(
            "task_execution_fences",
            sa.Column("expected_placement_version", sa.BigInteger()),
        )
        op.add_column("task_execution_fences", sa.Column("catalog_version_id", sa.Uuid()))
        op.create_foreign_key(
            "fk_task_fence_catalog_version",
            "task_execution_fences",
            "section_catalog_versions",
            ["catalog_version_id"],
            ["catalog_version_id"],
            ondelete="RESTRICT",
        )
        op.create_check_constraint(
            "ck_task_execution_fences_expected_placement_positive",
            "task_execution_fences",
            "expected_placement_version IS NULL OR expected_placement_version > 0",
        )

        for table, constraint in (
            ("workflow_operations", "fk_workflow_operation_catalog_version"),
            ("verification_inspection_occurrences", "fk_verification_inspection_catalog_version"),
        ):
            op.add_column(table, sa.Column("catalog_version_id", sa.Uuid()))
            op.create_foreign_key(
                constraint,
                table,
                "section_catalog_versions",
                ["catalog_version_id"],
                ["catalog_version_id"],
                ondelete="RESTRICT",
            )
        return

    triggers = _suspend_sqlite_triggers("dish_states")
    try:
        with op.batch_alter_table("dish_states") as batch:
            batch.add_column(sa.Column("catalog_version_id", sa.Uuid()))
            batch.create_foreign_key(
                "fk_dish_states_catalog_version",
                "section_catalog_versions",
                ["catalog_version_id"],
                ["catalog_version_id"],
                ondelete="RESTRICT",
            )
    finally:
        _restore_sqlite_triggers(triggers)

    triggers = _suspend_sqlite_triggers("task_execution_fences")
    try:
        with op.batch_alter_table("task_execution_fences") as batch:
            batch.add_column(sa.Column("expected_placement_version", sa.BigInteger()))
            batch.add_column(sa.Column("catalog_version_id", sa.Uuid()))
            batch.create_foreign_key(
                "fk_task_fence_catalog_version",
                "section_catalog_versions",
                ["catalog_version_id"],
                ["catalog_version_id"],
                ondelete="RESTRICT",
            )
            batch.create_check_constraint(
                "ck_task_execution_fences_expected_placement_positive",
                "expected_placement_version IS NULL OR expected_placement_version > 0",
            )
    finally:
        _restore_sqlite_triggers(triggers)

    for table, constraint in (
        ("workflow_operations", "fk_workflow_operation_catalog_version"),
        ("verification_inspection_occurrences", "fk_verification_inspection_catalog_version"),
    ):
        triggers = _suspend_sqlite_triggers(table)
        try:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("catalog_version_id", sa.Uuid()))
                batch.create_foreign_key(
                    constraint,
                    "section_catalog_versions",
                    ["catalog_version_id"],
                    ["catalog_version_id"],
                    ondelete="RESTRICT",
                )
        finally:
            _restore_sqlite_triggers(triggers)


def _drop_shared_columns() -> None:
    offline = context.is_offline_mode()
    if offline:
        for table, constraint in (
            ("verification_inspection_occurrences", "fk_verification_inspection_catalog_version"),
            ("workflow_operations", "fk_workflow_operation_catalog_version"),
        ):
            op.drop_constraint(
                constraint,
                table,
                type_="foreignkey",
            )
            op.drop_column(table, "catalog_version_id")
        op.drop_constraint(
            "ck_task_execution_fences_expected_placement_positive",
            "task_execution_fences",
            type_="check",
        )
        op.drop_constraint(
            "fk_task_fence_catalog_version",
            "task_execution_fences",
            type_="foreignkey",
        )
        op.drop_column("task_execution_fences", "catalog_version_id")
        op.drop_column("task_execution_fences", "expected_placement_version")
        op.drop_constraint(
            "fk_dish_states_catalog_version",
            "dish_states",
            type_="foreignkey",
        )
        op.drop_column("dish_states", "catalog_version_id")
        return

    for table, constraint in (
        ("verification_inspection_occurrences", "fk_verification_inspection_catalog_version"),
        ("workflow_operations", "fk_workflow_operation_catalog_version"),
    ):
        triggers = _suspend_sqlite_triggers(table)
        try:
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(
                    constraint,
                    type_="foreignkey",
                )
                batch.drop_column("catalog_version_id")
        finally:
            _restore_sqlite_triggers(triggers)
    triggers = _suspend_sqlite_triggers("task_execution_fences")
    try:
        with op.batch_alter_table("task_execution_fences") as batch:
            batch.drop_constraint(
                "ck_task_execution_fences_expected_placement_positive", type_="check"
            )
            batch.drop_constraint(
                "fk_task_fence_catalog_version",
                type_="foreignkey",
            )
            batch.drop_column("catalog_version_id")
            batch.drop_column("expected_placement_version")
    finally:
        _restore_sqlite_triggers(triggers)
    triggers = _suspend_sqlite_triggers("dish_states")
    try:
        with op.batch_alter_table("dish_states") as batch:
            batch.drop_constraint(
                "fk_dish_states_catalog_version",
                type_="foreignkey",
            )
            batch.drop_column("catalog_version_id")
    finally:
        _restore_sqlite_triggers(triggers)


def upgrade() -> None:
    op.create_table(
        "native_catalog_runtime_attestations",
        sa.Column("attestation_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_attestation_id", sa.Uuid()),
        sa.Column("baseline_migration_event_id", sa.Uuid()),
        sa.Column("attestation_revision", sa.BigInteger(), nullable=False),
        sa.Column("attestation_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attestation_revision > 0", name="ck_native_catalog_runtime_attestations_positive_revision"),
        sa.CheckConstraint("length(attestation_sha256) = 64", name="ck_native_catalog_runtime_attestations_attestation_hash_length"),
        sa.CheckConstraint(
            "(attestation_revision = 1 AND predecessor_attestation_id IS NULL AND baseline_migration_event_id IS NOT NULL) OR "
            "(attestation_revision > 1 AND predecessor_attestation_id IS NOT NULL AND baseline_migration_event_id IS NULL)",
            name="ck_native_catalog_runtime_attestations_exact_root_or_successor_shape",
        ),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["section_catalog_versions.catalog_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["catalog_activation_id"], ["section_catalog_activations.catalog_activation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["predecessor_attestation_id"], ["native_catalog_runtime_attestations.attestation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["baseline_migration_event_id"], ["applied_migration_events.migration_event_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("attestation_id"),
        sa.UniqueConstraint("generation_id", "attestation_revision", name="uq_attestation_generation_revision"),
        sa.UniqueConstraint("generation_id", "catalog_activation_id", name="uq_attestation_generation_activation"),
        sa.UniqueConstraint(
            "attestation_id",
            "generation_id",
            "catalog_version_id",
            "catalog_activation_id",
            "attestation_revision",
            name="uq_attestation_exact_identity",
        ),
    )
    op.create_table(
        "current_native_catalog_runtimes",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("attestation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("attestation_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attestation_revision > 0", name="ck_current_native_catalog_runtimes_positive_revision"),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["attestation_id", "generation_id", "catalog_version_id", "catalog_activation_id", "attestation_revision"],
            [
                "native_catalog_runtime_attestations.attestation_id",
                "native_catalog_runtime_attestations.generation_id",
                "native_catalog_runtime_attestations.catalog_version_id",
                "native_catalog_runtime_attestations.catalog_activation_id",
                "native_catalog_runtime_attestations.attestation_revision",
            ],
            name="fk_current_native_runtime_exact_attestation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("generation_id"),
        sa.UniqueConstraint("attestation_id"),
    )
    _add_shared_columns()

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER native_catalog_runtime_attestations_immutable "
            "BEFORE UPDATE OR DELETE ON native_catalog_runtime_attestations "
            "FOR EACH ROW EXECUTE FUNCTION dish_reject_immutable_authority()"
        )
        op.execute(
            "CREATE TRIGGER current_native_catalog_runtimes_delete_forbidden "
            "BEFORE DELETE ON current_native_catalog_runtimes "
            "FOR EACH ROW EXECUTE FUNCTION dish_reject_immutable_authority()"
        )
    else:
        op.execute(
            "CREATE TRIGGER native_catalog_runtime_attestations_immutable_update "
            "BEFORE UPDATE ON native_catalog_runtime_attestations "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )
        op.execute(
            "CREATE TRIGGER native_catalog_runtime_attestations_immutable_delete "
            "BEFORE DELETE ON native_catalog_runtime_attestations "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )
        op.execute(
            "CREATE TRIGGER current_native_catalog_runtimes_delete_forbidden "
            "BEFORE DELETE ON current_native_catalog_runtimes "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )


def downgrade() -> None:
    if not context.is_offline_mode():
        for table in ("current_native_catalog_runtimes", "native_catalog_runtime_attestations"):
            if op.get_bind().exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first():
                raise RuntimeError(
                    "0049_native_catalog_runtime_authority_root downgrade refuses established runtime authority"
                )
    _drop_shared_columns()
    op.drop_table("current_native_catalog_runtimes")
    op.drop_table("native_catalog_runtime_attestations")
