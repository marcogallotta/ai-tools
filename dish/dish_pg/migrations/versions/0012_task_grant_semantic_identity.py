"""Prohibit duplicate task-level Marco authorization grants.

Revision ID: 0012_task_grant_semantic_identity
Revises: 0011_rollback_bundle_identity
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_task_grant_semantic_identity"
down_revision = "0011_rollback_bundle_identity"
branch_labels = None
depends_on = None

_INDEX = "uq_marco_grant_task_semantic_identity"
_COLUMNS = (
    "generation_id",
    "task_id",
    "field_name",
    "before_value",
    "after_value",
)


def upgrade() -> None:
    op.create_index(
        op.f(_INDEX),
        "marco_authorization_grants",
        _COLUMNS,
        unique=True,
        postgresql_where=sa.text("operation_id IS NULL"),
        sqlite_where=sa.text("operation_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f(_INDEX), table_name="marco_authorization_grants")
