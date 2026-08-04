"""Complete successfully published abandonment attempts.

Revision ID: 0017_abandonment_terminal_state
Revises: 0016_honest_binding_null_identity
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_abandonment_terminal_state"
down_revision = "0016_honest_binding_null_identity"
branch_labels = None
depends_on = None

_TABLE = "abandonment_attempts"
_CHECK = "ck_abandonment_attempts_state_payload_consistent"
_EXPRESSION = (
    "(state IN ('preparing','blocked','reconciling') "
    "AND successor_operation_id IS NULL AND terminal_at IS NULL) OR "
    "(state = 'published' AND successor_operation_id IS NOT NULL AND terminal_at IS NULL) OR "
    "(state = 'completed' AND successor_operation_id IS NOT NULL AND terminal_at IS NOT NULL) OR "
    "(state = 'cancelled' AND terminal_at IS NOT NULL)"
)
_OLD_EXPRESSION = (
    "(state IN ('preparing','blocked','reconciling') AND terminal_at IS NULL) OR "
    "(state = 'published' AND successor_operation_id IS NOT NULL AND terminal_at IS NULL) OR "
    "(state IN ('completed','cancelled') AND terminal_at IS NOT NULL)"
)


def upgrade() -> None:
    # A published row is a completed abandonment only when the durable
    # succession edge binds this exact attempt to this exact successor.
    op.execute(
        sa.text(
            """
            UPDATE abandonment_attempts
               SET state = 'completed',
                   terminal_at = (
                       SELECT edge.published_at
                         FROM operation_succession_edges AS edge
                        WHERE edge.abandonment_id = abandonment_attempts.abandonment_id
                          AND edge.successor_operation_id = abandonment_attempts.successor_operation_id
                   )
             WHERE state = 'published'
               AND successor_operation_id IS NOT NULL
               AND EXISTS (
                   SELECT 1
                     FROM operation_succession_edges AS edge
                    WHERE edge.abandonment_id = abandonment_attempts.abandonment_id
                      AND edge.successor_operation_id = abandonment_attempts.successor_operation_id
               )
            """
        )
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(op.f(_CHECK), type_="check")
            batch.create_check_constraint(op.f(_CHECK), _EXPRESSION)
        return
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, _EXPRESSION)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(op.f(_CHECK), type_="check")
            batch.create_check_constraint(op.f(_CHECK), _OLD_EXPRESSION)
        return
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, _OLD_EXPRESSION)
