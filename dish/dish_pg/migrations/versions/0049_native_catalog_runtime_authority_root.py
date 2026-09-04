"""Establish the native-catalog runtime authority attestation root and current pointer.

This is PR2's authority-establishing schema.  Creating these tables does not by
itself switch runtime authority: a ``CurrentNativeCatalogRuntime`` row for a
generation exists only once the online root-establishment transaction commits
it alongside revision-1 ``NativeCatalogRuntimeAttestation`` and this same
migration's own generation-bound ``AppliedMigrationEvent``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0049_native_catalog_runtime_authority_root"
down_revision = "0048_native_section_content_carry_forward"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_catalog_runtime_attestations",
        sa.Column("attestation_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_attestation_id", sa.Uuid(), nullable=True),
        sa.Column("baseline_migration_event_id", sa.Uuid(), nullable=True),
        sa.Column("attestation_revision", sa.BigInteger(), nullable=False),
        sa.Column("attestation_sha256", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attestation_revision > 0",
            name=op.f("ck_native_catalog_runtime_attestations_positive_revision"),
        ),
        sa.CheckConstraint(
            "length(attestation_sha256) = 64",
            name=op.f("ck_native_catalog_runtime_attestations_attestation_hash_length"),
        ),
        sa.CheckConstraint(
            "(attestation_revision = 1 AND predecessor_attestation_id IS NULL "
            "AND baseline_migration_event_id IS NOT NULL) OR "
            "(attestation_revision > 1 AND predecessor_attestation_id IS NOT NULL "
            "AND baseline_migration_event_id IS NULL)",
            name=op.f("ck_native_catalog_runtime_attestations_exact_root_or_successor_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            name=op.f("fk_native_catalog_runtime_attestations_generation_id_authority_generations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id"],
            ["section_catalog_versions.catalog_version_id"],
            name=op.f("fk_native_catalog_runtime_attestations_catalog_version_id_section_catalog_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_activation_id"],
            ["section_catalog_activations.catalog_activation_id"],
            name=op.f("fk_native_catalog_runtime_attestations_catalog_activation_id_section_catalog_activations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_attestation_id"],
            ["native_catalog_runtime_attestations.attestation_id"],
            name=op.f("fk_native_catalog_runtime_attestations_predecessor_attestation_id_native_catalog_runtime_attestations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_migration_event_id"],
            ["applied_migration_events.migration_event_id"],
            name=op.f("fk_native_catalog_runtime_attestations_baseline_migration_event_id_applied_migration_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attestation_id", name=op.f("pk_native_catalog_runtime_attestations")),
        sa.UniqueConstraint(
            "generation_id",
            "attestation_revision",
            name="uq_attestation_generation_revision",
        ),
        sa.UniqueConstraint(
            "generation_id",
            "catalog_activation_id",
            name="uq_attestation_generation_activation",
        ),
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
        sa.CheckConstraint(
            "attestation_revision > 0",
            name=op.f("ck_current_native_catalog_runtimes_positive_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            name=op.f("fk_current_native_catalog_runtimes_generation_id_authority_generations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "attestation_id",
                "generation_id",
                "catalog_version_id",
                "catalog_activation_id",
                "attestation_revision",
            ],
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
        sa.PrimaryKeyConstraint("generation_id", name=op.f("pk_current_native_catalog_runtimes")),
        sa.UniqueConstraint(
            "attestation_id",
            name=op.f("uq_current_native_catalog_runtimes_attestation_id"),
        ),
    )

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
        for table in ("native_catalog_runtime_attestations", "current_native_catalog_runtimes"):
            populated = op.get_bind().exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first()
            if populated is not None:
                raise RuntimeError(
                    "0049_native_catalog_runtime_authority_root downgrade refuses an established runtime root"
                )
    op.drop_table("current_native_catalog_runtimes")
    op.drop_table("native_catalog_runtime_attestations")
