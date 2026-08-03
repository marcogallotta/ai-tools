"""Add deterministic per-operation Verification cycle ordering.

Revision ID: 0015_verification_cycle_sequence
Revises: 0014_projection_outbox_origin
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_verification_cycle_sequence"
down_revision = "0014_projection_outbox_origin"
branch_labels = None
depends_on = None

_CHECK = "ck_verification_cycles_positive_cycle_sequence"
_UNIQUE = "uq_verification_cycle_sequence"


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "verification_cycles",
        sa.Column("cycle_sequence", sa.BigInteger(), nullable=True),
    )

    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    cycle_id,
                    row_number() OVER (
                        PARTITION BY operation_id
                        ORDER BY created_at, cycle_id
                    ) AS sequence_value
                FROM verification_cycles
            )
            UPDATE verification_cycles
               SET cycle_sequence = (
                   SELECT ranked.sequence_value
                     FROM ranked
                    WHERE ranked.cycle_id = verification_cycles.cycle_id
               )
            """
        )
    )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("verification_cycles") as batch:
            batch.alter_column(
                "cycle_sequence",
                existing_type=sa.BigInteger(),
                nullable=False,
            )
            batch.create_check_constraint(_CHECK, "cycle_sequence > 0")
            batch.create_unique_constraint(
                _UNIQUE, ("operation_id", "cycle_sequence")
            )
        return

    op.alter_column(
        "verification_cycles",
        "cycle_sequence",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_check_constraint(
        _CHECK, "verification_cycles", "cycle_sequence > 0"
    )
    op.create_unique_constraint(
        _UNIQUE,
        "verification_cycles",
        ("operation_id", "cycle_sequence"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("verification_cycles") as batch:
            batch.drop_constraint(_UNIQUE, type_="unique")
            batch.drop_constraint(_CHECK, type_="check")
            batch.drop_column("cycle_sequence")
        return

    op.drop_constraint(_UNIQUE, "verification_cycles", type_="unique")
    op.drop_constraint(_CHECK, "verification_cycles", type_="check")
    op.drop_column("verification_cycles", "cycle_sequence")
