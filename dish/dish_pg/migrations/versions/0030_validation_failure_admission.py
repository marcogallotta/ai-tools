"""Allow durable pre-execution validation failures without mutation admission.

Revision ID: 0030_validation_failure_admission
Revises: 0029_cutover_authority_admission_fixes
"""
from __future__ import annotations

import importlib

from alembic import op

revision = "0030_validation_failure_admission"
down_revision = "0029_cutover_authority_admission_fixes"
branch_labels = None
depends_on = None


def _previous_revision():
    return importlib.import_module(
        "dish_pg.migrations.versions.0029_cutover_authority_admission_fixes"
    )


def upgrade() -> None:
    previous = _previous_revision()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        previous._install_postgresql_admission_function(
            isolated_first_request=True,
            allow_validation_failures=True,
        )
    elif bind.dialect.name == "sqlite":
        previous._install_sqlite_admission_guard(
            allow_validation_failures=True,
        )


def downgrade() -> None:
    previous = _previous_revision()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        previous._install_postgresql_admission_function(
            isolated_first_request=True,
            allow_validation_failures=False,
        )
    elif bind.dialect.name == "sqlite":
        previous._install_sqlite_admission_guard(
            allow_validation_failures=False,
        )
