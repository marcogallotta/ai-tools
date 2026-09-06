"""Reserve PR2f's migration identity without making schema migration the authority switch."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0050_native_catalog_runtime_authority_switch"
down_revision = "0049_native_catalog_runtime_authority_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reaching the Alembic schema head is deployment bookkeeping only. The accepted
    # PR2f boundary requires the authority switch to be an explicit caller-owned
    # transaction through finalize_native_catalog_runtime_authority(); routine schema
    # migration, offline SQL rendering, startup migration, reset/rehearsal migration,
    # or a test fixture must never establish CurrentNativeCatalogRuntime implicitly.
    #
    # The finalizer records its own generation-bound AppliedMigrationEvent with this
    # exact revision/code identity when the authorized switch is actually executed.
    return


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    switched = bind.execute(
        sa.text("SELECT 1 FROM current_native_catalog_runtimes LIMIT 1")
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
