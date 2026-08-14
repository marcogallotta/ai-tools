"""Allow the TEST-only fixture-recovery authority generation reason.

Revision ID: 0041_test_generation_rollover
Revises: 0040_no_asana_post_burn
"""
from __future__ import annotations

from alembic import op

revision = "0041_test_generation_rollover"
down_revision = "0040_no_asana_post_burn"
branch_labels = None
depends_on = None


def _create_current_constraints(batch) -> None:
    batch.create_check_constraint(
        "creation_reason_allowed",
        "creation_reason IN ('initial_cutover','destructive_restore','test_fixture_recovery')",
    )
    batch.create_check_constraint(
        "creation_provenance_complete",
        "(creation_reason = 'initial_cutover' AND predecessor_generation_id IS NULL "
        "AND external_restore_control_id IS NULL) OR "
        "(creation_reason = 'destructive_restore' AND predecessor_generation_id IS NOT NULL "
        "AND external_restore_control_id IS NOT NULL) OR "
        "(creation_reason = 'test_fixture_recovery' AND predecessor_generation_id IS NOT NULL "
        "AND external_restore_control_id IS NULL)",
    )


def _create_previous_constraints(batch) -> None:
    batch.create_check_constraint(
        "creation_reason_allowed",
        "creation_reason IN ('initial_cutover','destructive_restore')",
    )
    batch.create_check_constraint(
        "creation_provenance_complete",
        "(creation_reason = 'initial_cutover' AND predecessor_generation_id IS NULL "
        "AND external_restore_control_id IS NULL) OR "
        "(creation_reason = 'destructive_restore' AND predecessor_generation_id IS NOT NULL "
        "AND external_restore_control_id IS NOT NULL)",
    )


def upgrade() -> None:
    with op.batch_alter_table("authority_generations") as batch:
        batch.drop_constraint(
            "creation_reason_allowed", type_="check"
        )
        batch.drop_constraint(
            "creation_provenance_complete", type_="check"
        )
        _create_current_constraints(batch)


def downgrade() -> None:
    bind = op.get_bind()
    count = int(
        bind.exec_driver_sql(
            "SELECT count(*) FROM authority_generations "
            "WHERE creation_reason = 'test_fixture_recovery'"
        ).scalar_one()
    )
    if count:
        raise RuntimeError(
            "refusing lossy downgrade: test_fixture_recovery authority generations exist"
        )
    with op.batch_alter_table("authority_generations") as batch:
        batch.drop_constraint(
            "creation_reason_allowed", type_="check"
        )
        batch.drop_constraint(
            "creation_provenance_complete", type_="check"
        )
        _create_previous_constraints(batch)
