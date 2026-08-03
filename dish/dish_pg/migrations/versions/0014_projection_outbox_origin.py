"""Tag projection outbox rows with live or shadow origin.

Revision ID: 0014_projection_outbox_origin
Revises: 0013_dark_launch_shadow_capture
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_projection_outbox_origin"
down_revision = "0013_dark_launch_shadow_capture"
branch_labels = None
depends_on = None


def _replace_identity_trigger(*, include_origin: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER projection_outbox_identity_update ON projection_outbox_events")
    columns = (
        "generation_id, projection_epoch_id, source_route, "
        + ("origin, " if include_origin else "")
        + "command_execution_id, task_id, event_type, aggregate_sequence, "
        "idempotency_key, intent_payload, intent_sha256, created_at"
    )
    op.execute(
        "CREATE TRIGGER projection_outbox_identity_update "
        f"BEFORE UPDATE OF {columns} "
        "ON projection_outbox_events FOR EACH ROW "
        "EXECUTE FUNCTION dish_reject_projection_outbox_identity_update()"
    )


def upgrade() -> None:
    with op.batch_alter_table("projection_outbox_events") as batch:
        batch.add_column(
            sa.Column(
                "origin",
                sa.String(length=16),
                nullable=False,
                server_default="live",
            )
        )
        batch.create_check_constraint(
            "ck_projection_outbox_events_origin_allowed",
            "origin IN ('live','shadow')",
        )
    _replace_identity_trigger(include_origin=True)


def downgrade() -> None:
    _replace_identity_trigger(include_origin=False)
    with op.batch_alter_table("projection_outbox_events") as batch:
        batch.drop_constraint("ck_projection_outbox_events_origin_allowed", type_="check")
        batch.drop_column("origin")
