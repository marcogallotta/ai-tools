"""Allow complete Human Review decisions instead of truncating them at 32 characters.

Revision ID: 0055_human_review_decision_text
Revises: 0054_authorization_consumed_result_not_unique
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_human_review_decision_text"
down_revision = "0054_authorization_consumed_result_not_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("human_review_decisions") as batch:
        batch.alter_column(
            "decision",
            existing_type=sa.String(length=32),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("human_review_decisions") as batch:
        batch.alter_column(
            "decision",
            existing_type=sa.Text(),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
