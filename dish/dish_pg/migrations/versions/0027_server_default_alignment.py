"""Align durable database defaults with ORM server-default metadata.

Revision ID: 0027_server_default_alignment
Revises: 0026_typed_worker_readiness_evidence
"""
from __future__ import annotations

from alembic import op

revision = "0027_server_default_alignment"
down_revision = "0026_typed_worker_readiness_evidence"
branch_labels = None
depends_on = None


# All four defaults are durable raw-SQL insertion contracts, not temporary
# backfill defaults.  Earlier migrations intentionally retained them.
def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE shadow_envelopes ALTER COLUMN capture_qualification SET DEFAULT 'legacy'")
        op.execute("ALTER TABLE shadow_envelopes ALTER COLUMN envelope_schema_version SET DEFAULT 1")
        op.execute("ALTER TABLE projection_epochs ALTER COLUMN external_effects_enabled SET DEFAULT false")
        op.execute("ALTER TABLE projection_outbox_events ALTER COLUMN origin SET DEFAULT 'live'")


def downgrade() -> None:
    # The predecessor schema already carries these durable defaults.  A downgrade
    # therefore preserves them rather than silently weakening raw-SQL behavior.
    return None
