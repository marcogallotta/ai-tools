"""Stop requiring a globally unique consumed_result_id per authorization grant.

Revision ID: 0054_authorization_consumed_result_not_unique
Revises: 0053_native_section_lifecycle
"""
from __future__ import annotations

from alembic import op

revision = "0054_authorization_consumed_result_not_unique"
down_revision = "0053_native_section_lifecycle"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_marco_authorization_states_consumed_result_id"


def upgrade() -> None:
    with op.batch_alter_table("marco_authorization_states") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="unique")


def downgrade() -> None:
    with op.batch_alter_table("marco_authorization_states") as batch:
        batch.create_unique_constraint(_CONSTRAINT, ["consumed_result_id"])
