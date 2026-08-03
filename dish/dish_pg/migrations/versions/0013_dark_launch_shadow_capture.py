"""Add dark-launch capture identity and fail-closed projection delivery.

Revision ID: 0013_dark_launch_shadow_capture
Revises: 0012_task_grant_semantic_identity
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_dark_launch_shadow_capture"
down_revision = "0012_task_grant_semantic_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Batch mode preserves the repository's executable SQLite migration lane
    # while rendering ordinary ALTER TABLE statements on PostgreSQL.
    with op.batch_alter_table("shadow_envelopes") as batch:
        batch.add_column(sa.Column("rollout_sequence", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("source_authority_generation", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("source_execution_identity", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("principal", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("source_pre_state", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("source_pre_state_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("pinned_inputs", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("source_effects", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("capture_qualification", sa.String(length=24), nullable=False, server_default="legacy"))
        batch.add_column(sa.Column("source_post_state_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("envelope_schema_version", sa.Integer(), nullable=False, server_default="1"))
        batch.create_check_constraint(
            "ck_shadow_envelopes_pre_state_hash_length",
            "source_pre_state_sha256 IS NULL OR length(source_pre_state_sha256) = 64",
        )
        batch.create_check_constraint(
            "ck_shadow_envelopes_post_state_hash_length",
            "source_post_state_sha256 IS NULL OR length(source_post_state_sha256) = 64",
        )
        batch.create_check_constraint(
            "ck_shadow_envelopes_positive_rollout_sequence",
            "rollout_sequence IS NULL OR rollout_sequence > 0",
        )
        batch.create_check_constraint(
            "ck_shadow_envelopes_capture_qualification_allowed",
            "capture_qualification IN ('legacy','execute','capture_only','excluded')",
        )
        batch.create_check_constraint(
            "ck_shadow_envelopes_positive_schema_version",
            "envelope_schema_version > 0",
        )
    op.create_index(
        "uq_shadow_rollout_sequence",
        "shadow_envelopes",
        ["shadow_baseline_id", "rollout_sequence"],
        unique=True,
        postgresql_where=sa.text("rollout_sequence IS NOT NULL"),
        sqlite_where=sa.text("rollout_sequence IS NOT NULL"),
    )
    with op.batch_alter_table("projection_epochs") as batch:
        batch.add_column(
            sa.Column("external_effects_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("projection_epochs") as batch:
        batch.drop_column("external_effects_enabled")
    op.drop_index("uq_shadow_rollout_sequence", table_name="shadow_envelopes")
    with op.batch_alter_table("shadow_envelopes") as batch:
        for name in (
            "ck_shadow_envelopes_positive_schema_version",
            "ck_shadow_envelopes_capture_qualification_allowed",
            "ck_shadow_envelopes_positive_rollout_sequence",
            "ck_shadow_envelopes_post_state_hash_length",
            "ck_shadow_envelopes_pre_state_hash_length",
        ):
            batch.drop_constraint(name, type_="check")
        for column in (
            "envelope_schema_version",
            "source_post_state_sha256",
            "capture_qualification",
            "source_effects",
            "pinned_inputs",
            "source_pre_state_sha256",
            "source_pre_state",
            "principal",
            "source_execution_identity",
            "source_authority_generation",
            "rollout_sequence",
        ):
            batch.drop_column(column)
