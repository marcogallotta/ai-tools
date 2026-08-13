"""Retire post-burn projection-worker runtime requirement without deleting history.

Revision ID: 0040_no_asana_post_burn
Revises: 0039_remove_unused_causality_edges
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_no_asana_post_burn"
down_revision = "0039_remove_unused_causality_edges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_release_attestations") as batch:
        batch.alter_column(
            "projection_worker_artifact_sha256",
            existing_type=sa.String(length=64),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    missing = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM runtime_release_attestations "
                "WHERE projection_worker_artifact_sha256 IS NULL"
            )
        ).scalar_one()
    )
    if missing:
        raise RuntimeError(
            "refusing lossy downgrade: no-Asana runtime attestations lack worker artifacts"
        )
    with op.batch_alter_table("runtime_release_attestations") as batch:
        batch.alter_column(
            "projection_worker_artifact_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
        )
