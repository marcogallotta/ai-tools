"""Fence projection attempts and version retry dispatch identities.

Revision ID: 0018_projection_attempt_lifecycle
Revises: 0017_abandonment_terminal_state
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_projection_attempt_lifecycle"
down_revision = "0017_abandonment_terminal_state"
branch_labels = None
depends_on = None

_OLD_REQUEST_UNIQUE = "uq_projection_attempts_request_identity"
_DISPATCH_UNIQUE = "uq_projection_attempts_dispatch_identity"
_POSITIVE = "ck_projection_attempts_positive_retry_generation"
_CLAIM = "ck_projection_attempts_dispatch_claim_consistent"
_HASH = "ck_projection_attempts_dispatch_identity_length"
_KIND = "ck_projection_attempts_attempt_kind_allowed"
_PREDECESSOR = "ck_projection_attempts_predecessor_consistent"
_PREDECESSOR_FK = "fk_projection_attempts_predecessor_attempt_id"


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "projection_attempts",
        sa.Column("attempt_kind", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "projection_attempts",
        sa.Column("predecessor_attempt_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "projection_attempts",
        sa.Column("dispatch_identity", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "projection_attempts",
        sa.Column("retry_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "projection_attempts",
        sa.Column("dispatch_claim_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "projection_attempts",
        sa.Column("dispatch_claim_revision", sa.BigInteger(), nullable=True),
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE projection_attempts
                   SET attempt_kind = 'dispatch',
                       dispatch_identity =
                       lower(replace(attempt_id::text, '-', '')) || substr(request_sha256, 1, 32),
                       retry_generation = attempt_number
                """
            )
        )
    else:
        op.execute(
            sa.text(
                """
                UPDATE projection_attempts
                   SET attempt_kind = 'dispatch',
                       dispatch_identity =
                       lower(replace(attempt_id, '-', '')) || substr(request_sha256, 1, 32),
                       retry_generation = attempt_number
                """
            )
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projection_attempts") as batch:
            batch.drop_constraint(_OLD_REQUEST_UNIQUE, type_="unique")
            batch.alter_column("attempt_kind", existing_type=sa.String(16), nullable=False)
            batch.alter_column("dispatch_identity", existing_type=sa.String(64), nullable=False)
            batch.alter_column("retry_generation", existing_type=sa.Integer(), nullable=False)
            batch.create_unique_constraint(_DISPATCH_UNIQUE, ("dispatch_identity",))
            batch.create_foreign_key(
                _PREDECESSOR_FK, "projection_attempts", ("predecessor_attempt_id",), ("attempt_id",), ondelete="RESTRICT"
            )
            batch.create_check_constraint(_KIND, "attempt_kind IN ('dispatch','recovery')")
            batch.create_check_constraint(
                _PREDECESSOR,
                "(attempt_kind = 'dispatch' AND predecessor_attempt_id IS NULL) OR "
                "(attempt_kind = 'recovery' AND predecessor_attempt_id IS NOT NULL)",
            )
            batch.create_check_constraint(_POSITIVE, "retry_generation > 0")
            batch.create_check_constraint(_HASH, "length(dispatch_identity) = 64")
            batch.create_check_constraint(
                _CLAIM,
                "(dispatch_claim_token IS NULL AND dispatch_claim_revision IS NULL) OR "
                "(dispatch_claim_token IS NOT NULL AND dispatch_claim_revision > 0)",
            )
        return

    op.drop_constraint(_OLD_REQUEST_UNIQUE, "projection_attempts", type_="unique")
    op.alter_column(
        "projection_attempts", "attempt_kind", existing_type=sa.String(16), nullable=False
    )
    op.alter_column(
        "projection_attempts", "dispatch_identity", existing_type=sa.String(64), nullable=False
    )
    op.alter_column(
        "projection_attempts", "retry_generation", existing_type=sa.Integer(), nullable=False
    )
    op.create_unique_constraint(_DISPATCH_UNIQUE, "projection_attempts", ("dispatch_identity",))
    op.create_foreign_key(
        _PREDECESSOR_FK, "projection_attempts", "projection_attempts", ("predecessor_attempt_id",), ("attempt_id",), ondelete="RESTRICT"
    )
    op.create_check_constraint(_KIND, "projection_attempts", "attempt_kind IN ('dispatch','recovery')")
    op.create_check_constraint(
        _PREDECESSOR,
        "projection_attempts",
        "(attempt_kind = 'dispatch' AND predecessor_attempt_id IS NULL) OR "
        "(attempt_kind = 'recovery' AND predecessor_attempt_id IS NOT NULL)",
    )
    op.create_check_constraint(_POSITIVE, "projection_attempts", "retry_generation > 0")
    op.create_check_constraint(_HASH, "projection_attempts", "length(dispatch_identity) = 64")
    op.create_check_constraint(
        _CLAIM,
        "projection_attempts",
        "(dispatch_claim_token IS NULL AND dispatch_claim_revision IS NULL) OR "
        "(dispatch_claim_token IS NOT NULL AND dispatch_claim_revision > 0)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            """
            SELECT request_identity
              FROM projection_attempts
             GROUP BY request_identity
            HAVING count(*) > 1
             LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade projection retry generations with repeated request_identity history"
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projection_attempts") as batch:
            batch.drop_constraint(_CLAIM, type_="check")
            batch.drop_constraint(_PREDECESSOR, type_="check")
            batch.drop_constraint(_KIND, type_="check")
            batch.drop_constraint(_PREDECESSOR_FK, type_="foreignkey")
            batch.drop_constraint(_HASH, type_="check")
            batch.drop_constraint(_POSITIVE, type_="check")
            batch.drop_constraint(_DISPATCH_UNIQUE, type_="unique")
            batch.create_unique_constraint(_OLD_REQUEST_UNIQUE, ("request_identity",))
            batch.drop_column("dispatch_claim_revision")
            batch.drop_column("predecessor_attempt_id")
            batch.drop_column("attempt_kind")
            batch.drop_column("dispatch_claim_token")
            batch.drop_column("retry_generation")
            batch.drop_column("dispatch_identity")
        return

    op.drop_constraint(_CLAIM, "projection_attempts", type_="check")
    op.drop_constraint(_PREDECESSOR, "projection_attempts", type_="check")
    op.drop_constraint(_KIND, "projection_attempts", type_="check")
    op.drop_constraint(_PREDECESSOR_FK, "projection_attempts", type_="foreignkey")
    op.drop_constraint(_HASH, "projection_attempts", type_="check")
    op.drop_constraint(_POSITIVE, "projection_attempts", type_="check")
    op.drop_constraint(_DISPATCH_UNIQUE, "projection_attempts", type_="unique")
    op.create_unique_constraint(_OLD_REQUEST_UNIQUE, "projection_attempts", ("request_identity",))
    op.drop_column("projection_attempts", "dispatch_claim_revision")
    op.drop_column("projection_attempts", "predecessor_attempt_id")
    op.drop_column("projection_attempts", "attempt_kind")
    op.drop_column("projection_attempts", "dispatch_claim_token")
    op.drop_column("projection_attempts", "retry_generation")
    op.drop_column("projection_attempts", "dispatch_identity")
