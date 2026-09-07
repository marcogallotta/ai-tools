"""Enforce canonical DishState Section placement after native root establishment."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0052_dish_state_section_not_null"
down_revision = "0051_native_dish_state_placement"
branch_labels = None
depends_on = None


def _require_repaired_root() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    row_count = int(
        bind.execute(sa.text("SELECT count(*) FROM dish_states")).scalar_one()
    )
    null_count = int(
        bind.execute(
            sa.text("SELECT count(*) FROM dish_states WHERE section_id IS NULL")
        ).scalar_one()
    )
    if null_count:
        raise RuntimeError(
            "0052_dish_state_section_not_null refuses unrepaired NULL placement rows"
        )
    if row_count and bind.execute(
        sa.text("SELECT 1 FROM current_native_catalog_runtimes LIMIT 1")
    ).first() is None:
        raise RuntimeError(
            "0052_dish_state_section_not_null requires the native runtime root for populated state"
        )


def _require_no_runtime_root() -> None:
    if context.is_offline_mode():
        return
    if op.get_bind().execute(
        sa.text("SELECT 1 FROM current_native_catalog_runtimes LIMIT 1")
    ).first():
        raise RuntimeError(
            "0052_dish_state_section_not_null downgrade refuses established native runtime authority"
        )


def _suspend_sqlite_triggers() -> tuple[str, ...]:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE '%dish_states%'"
    ).all()
    for name, _sql in rows:
        connection.exec_driver_sql(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
    return tuple(sql for _name, sql in rows if sql)


def _restore_sqlite_triggers(statements: tuple[str, ...]) -> None:
    for statement in statements:
        op.get_bind().exec_driver_sql(statement)


def _set_nullable(nullable: bool) -> None:
    if context.is_offline_mode() or op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "dish_states",
            "section_id",
            existing_type=sa.Uuid(),
            nullable=nullable,
        )
        return
    triggers = _suspend_sqlite_triggers()
    try:
        with op.batch_alter_table("dish_states") as batch:
            batch.alter_column(
                "section_id",
                existing_type=sa.Uuid(),
                nullable=nullable,
            )
    finally:
        _restore_sqlite_triggers(triggers)


def upgrade() -> None:
    _require_repaired_root()
    _set_nullable(False)


def downgrade() -> None:
    _require_no_runtime_root()
    _set_nullable(True)
