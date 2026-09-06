"""Establish native Section runtime authority only on an already-populated active generation."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.orm import Session

revision = "0050_native_catalog_runtime_authority_switch"
down_revision = "0049_native_catalog_runtime_authority_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        # Offline SQL may advance schema bookkeeping but never creates the runtime root.
        return
    bind = op.get_bind()
    active = bind.execute(
        sa.text("SELECT generation_id FROM authority_generations WHERE status='active'")
    ).all()
    if not active:
        # Fresh/empty databases acquire the schema head without claiming runtime authority.
        return
    from dish_pg.native_catalog_runtime_finalizer import (
        finalize_native_catalog_runtime_authority,
    )

    session = Session(bind=bind, autoflush=False, expire_on_commit=False, future=True)
    finalize_native_catalog_runtime_authority(session)
    session.flush()


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    switched = bind.execute(
        sa.text(
            "SELECT 1 FROM current_native_catalog_runtimes LIMIT 1"
        )
    ).first()
    witnessed = bind.execute(
        sa.text(
            "SELECT 1 FROM applied_migration_events "
            "WHERE revision=:revision AND outcome='applied' LIMIT 1"
        ),
        {"revision": revision},
    ).first()
    if switched is not None or witnessed is not None:
        raise RuntimeError(
            "0050_native_catalog_runtime_authority_switch downgrade refuses established native runtime authority"
        )
