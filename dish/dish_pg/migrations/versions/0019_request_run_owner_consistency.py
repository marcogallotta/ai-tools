"""Enforce exact service-request ownership lineage.

Revision ID: 0019_request_run_owner_consistency
Revises: 0018_projection_attempt_lifecycle
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_request_run_owner_consistency"
down_revision = "0018_projection_attempt_lifecycle"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "fk_service_requests_exact_run_owner"


def _mismatched_request_count() -> int:
    if op.get_context().as_sql:
        return 0
    bind = op.get_bind()
    return int(
        bind.execute(
            sa.text(
                """
                SELECT count(*)
                FROM service_requests AS request
                LEFT JOIN service_runs AS run
                  ON run.generation_id = request.generation_id
                 AND run.owner_id = request.owner_id
                 AND run.run_id = request.run_id
                WHERE run.run_id IS NULL
                """
            )
        ).scalar_one()
    )


def upgrade() -> None:
    mismatches = _mismatched_request_count()
    if mismatches:
        raise RuntimeError(
            "cannot enforce request/run owner consistency: "
            f"{mismatches} predecessor service request row(s) reference a run owned by "
            "a different generation/owner; repair the lineage explicitly before retrying"
        )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # PostgreSQL is the authority for this remediation series. SQLite test
        # metadata receives the same composite FK through the ORM mapping.
        return
    op.create_foreign_key(
        CONSTRAINT_NAME,
        "service_requests",
        "service_runs",
        ["generation_id", "owner_id", "run_id"],
        ["generation_id", "owner_id", "run_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint(CONSTRAINT_NAME, "service_requests", type_="foreignkey")
