"""Require a nonblank exact rollback-burn bundle identity.

Revision ID: 0011_rollback_bundle_identity
Revises: 0010_release_chronology
"""
from __future__ import annotations

from alembic import op

revision = "0011_rollback_bundle_identity"
down_revision = "0010_release_chronology"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_authority_activations_legacy_bundle_nonblank"


def upgrade() -> None:
    with op.batch_alter_table("authority_activations") as batch:
        batch.create_check_constraint(
            op.f(_CONSTRAINT), "length(trim(legacy_bundle_id)) > 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("authority_activations") as batch:
        batch.drop_constraint(op.f(_CONSTRAINT), type_="check")
