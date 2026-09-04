"""Stage immutable native-Section content carry-forward evidence without switching runtime."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0048_native_section_content_carry_forward"
down_revision = "0047_native_section_catalog_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_section_content_carry_forward_occurrences",
        sa.Column("carry_forward_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("source_content_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_dish_version", sa.BigInteger(), nullable=False),
        sa.Column("source_content_identity", sa.String(256), nullable=False),
        sa.Column("source_status", sa.String(64)),
        sa.Column("target_catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("target_section_id", sa.Uuid(), nullable=False),
        sa.Column("destination_legacy_gid", sa.String(32), nullable=False),
        sa.Column("destination_display_name", sa.String(256), nullable=False),
        sa.Column("transformed_title", sa.Text(), nullable=False),
        sa.Column("transformed_body", sa.Text(), nullable=False),
        sa.Column("transformed_content_identity", sa.String(256), nullable=False),
        sa.Column("verification_baseline_kind", sa.String(32), nullable=False),
        sa.Column("verification_baseline_text", sa.Text()),
        sa.Column("transform_sha256", sa.String(64), nullable=False),
        sa.Column("import_run_id", sa.Uuid(), nullable=False),
        sa.Column("migration_event_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_dish_version > 0",
            name=op.f("ck_native_section_content_carry_forward_occurrences_positive_source_dish_version"),
        ),
        sa.CheckConstraint(
            "length(trim(destination_legacy_gid)) > 0",
            name=op.f("ck_native_section_content_carry_forward_occurrences_legacy_gid_nonblank"),
        ),
        sa.CheckConstraint(
            "length(trim(destination_display_name)) > 0",
            name=op.f("ck_native_section_content_carry_forward_occurrences_display_name_nonblank"),
        ),
        sa.CheckConstraint(
            "length(source_content_identity) > 0",
            name=op.f("ck_native_section_content_carry_forward_occurrences_source_identity_nonblank"),
        ),
        sa.CheckConstraint(
            "length(transformed_content_identity) > 0",
            name=op.f("ck_native_section_content_carry_forward_occurrences_transformed_identity_nonblank"),
        ),
        sa.CheckConstraint(
            "length(transform_sha256) = 64",
            name=op.f("ck_native_section_content_carry_forward_occurrences_transform_hash_length"),
        ),
        sa.CheckConstraint(
            "verification_baseline_kind IN ('none','migration_assigned_ready')",
            name=op.f("ck_native_section_content_carry_forward_occurrences_verification_baseline_kind_allowed"),
        ),
        sa.CheckConstraint(
            "(verification_baseline_kind = 'none' AND verification_baseline_text IS NULL) OR "
            "(verification_baseline_kind = 'migration_assigned_ready' AND verification_baseline_text IS NOT NULL)",
            name=op.f("ck_native_section_content_carry_forward_occurrences_verification_baseline_exact"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "source_content_version_id"],
            [
                "task_content_versions.generation_id",
                "task_content_versions.task_id",
                "task_content_versions.content_version_id",
            ],
            name="fk_native_section_carry_forward_exact_source_content",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "source_dish_version"],
            [
                "dish_mutation_receipts.generation_id",
                "dish_mutation_receipts.task_id",
                "dish_mutation_receipts.dish_version",
            ],
            name="fk_native_section_carry_forward_exact_source_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_catalog_version_id", "target_section_id"],
            [
                "section_catalog_entries.catalog_version_id",
                "section_catalog_entries.section_id",
            ],
            name="fk_native_section_carry_forward_exact_target_entry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["stage_a_import_runs.import_run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["migration_event_id"],
            ["applied_migration_events.migration_event_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "carry_forward_id",
            name=op.f("pk_native_section_content_carry_forward_occurrences"),
        ),
        sa.UniqueConstraint(
            "generation_id",
            "task_id",
            name="uq_native_section_carry_forward_task",
        ),
        sa.UniqueConstraint(
            "generation_id",
            "task_id",
            "source_content_version_id",
            name="uq_native_section_carry_forward_source_content",
        ),
    )
    op.create_index(
        "ix_native_section_carry_forward_target",
        "native_section_content_carry_forward_occurrences",
        ["generation_id", "target_catalog_version_id", "target_section_id", "task_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER native_section_content_carry_forward_immutable "
            "BEFORE UPDATE OR DELETE ON native_section_content_carry_forward_occurrences "
            "FOR EACH ROW EXECUTE FUNCTION dish_reject_immutable_authority()"
        )
    else:
        op.execute(
            "CREATE TRIGGER native_section_content_carry_forward_immutable_update "
            "BEFORE UPDATE ON native_section_content_carry_forward_occurrences "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )
        op.execute(
            "CREATE TRIGGER native_section_content_carry_forward_immutable_delete "
            "BEFORE DELETE ON native_section_content_carry_forward_occurrences "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )


def downgrade() -> None:
    if not context.is_offline_mode():
        populated = op.get_bind().exec_driver_sql(
            "SELECT 1 FROM native_section_content_carry_forward_occurrences LIMIT 1"
        ).first()
        if populated is not None:
            raise RuntimeError(
                "0048_native_section_content_carry_forward downgrade refuses staged carry-forward history"
            )
    op.drop_index(
        "ix_native_section_carry_forward_target",
        table_name="native_section_content_carry_forward_occurrences",
    )
    op.drop_table("native_section_content_carry_forward_occurrences")
