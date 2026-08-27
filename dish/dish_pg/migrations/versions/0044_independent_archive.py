"""Decouple PostgreSQL-native archive state from completion."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0044_independent_archive"
down_revision = "0043_archived_at"
branch_labels = None
depends_on = None

_EFFECT_CONSTRAINT = "ck_dish_mutation_receipts_at_least_one_effect"


def _require_no_archived_rows(*, direction: str) -> None:
    if context.is_offline_mode():
        return
    count = int(
        op.get_bind()
        .exec_driver_sql(
            "SELECT count(*) FROM dish_states WHERE archived_at IS NOT NULL"
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"0044_independent_archive {direction} refuses {count} populated archived row(s); "
            "repair from authoritative history before changing archive semantics"
        )


def _suspend_sqlite_receipt_triggers() -> tuple[str, ...]:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
        "AND sql LIKE '%dish_mutation_receipts%'"
    ).all()
    for name, _sql in rows:
        quoted = name.replace('"', '""')
        connection.exec_driver_sql(f'DROP TRIGGER "{quoted}"')
    return tuple(sql for _name, sql in rows)


def _restore_sqlite_triggers(statements: tuple[str, ...]) -> None:
    for statement in statements:
        op.get_bind().exec_driver_sql(statement)


def _add_archive_effect() -> None:
    if op.get_bind().dialect.name == "sqlite":
        triggers = _suspend_sqlite_receipt_triggers()
        try:
            with op.batch_alter_table("dish_mutation_receipts") as batch:
                batch.add_column(
                    sa.Column(
                        "archive_changed",
                        sa.Boolean(),
                        server_default=sa.false(),
                        nullable=False,
                    )
                )
                batch.drop_constraint(op.f(_EFFECT_CONSTRAINT), type_="check")
                batch.create_check_constraint(
                    op.f(_EFFECT_CONSTRAINT),
                    "content_changed OR placement_changed OR completion_changed OR archive_changed",
                )
        finally:
            _restore_sqlite_triggers(triggers)
        return
    op.add_column(
        "dish_mutation_receipts",
        sa.Column(
            "archive_changed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.drop_constraint(
        op.f(_EFFECT_CONSTRAINT), "dish_mutation_receipts", type_="check"
    )
    op.create_check_constraint(
        op.f(_EFFECT_CONSTRAINT),
        "dish_mutation_receipts",
        "content_changed OR placement_changed OR completion_changed OR archive_changed",
    )


def _drop_archive_effect() -> None:
    if op.get_bind().dialect.name == "sqlite":
        triggers = _suspend_sqlite_receipt_triggers()
        try:
            with op.batch_alter_table("dish_mutation_receipts") as batch:
                batch.drop_constraint(op.f(_EFFECT_CONSTRAINT), type_="check")
                batch.create_check_constraint(
                    op.f(_EFFECT_CONSTRAINT),
                    "content_changed OR placement_changed OR completion_changed",
                )
                batch.drop_column("archive_changed")
        finally:
            _restore_sqlite_triggers(triggers)
        return
    op.drop_constraint(
        op.f(_EFFECT_CONSTRAINT), "dish_mutation_receipts", type_="check"
    )
    op.create_check_constraint(
        op.f(_EFFECT_CONSTRAINT),
        "dish_mutation_receipts",
        "content_changed OR placement_changed OR completion_changed",
    )
    op.drop_column("dish_mutation_receipts", "archive_changed")


def _replace_postgresql_guard(*, independent: bool) -> None:
    archive_transition = (
        " OR receipt.archive_changed <> (NEW.archived_at IS DISTINCT FROM OLD.archived_at)"
        " OR (receipt.archive_changed AND receipt.source_route <> 'command_execution')"
        if independent
        else ""
    )
    archive_without_receipt = (
        "" if independent else " OR NEW.archived_at IS DISTINCT FROM OLD.archived_at"
    )
    initial_archive = " OR r.archive_changed" if independent else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION dish_validate_scalar_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE receipt dish_mutation_receipts%ROWTYPE;
        DECLARE content task_content_versions%ROWTYPE;
        DECLARE generation_reason text;
        BEGIN
          SELECT * INTO receipt FROM dish_mutation_receipts
           WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id AND dish_version=NEW.dish_version;
          IF receipt.dish_version IS NULL THEN RAISE EXCEPTION 'DishState receipt missing'; END IF;
          IF TG_OP='UPDATE' THEN
            IF NEW.dish_version <> OLD.dish_version + 1
               OR receipt.content_changed <> (NEW.current_content_version_id IS DISTINCT FROM OLD.current_content_version_id)
               OR receipt.placement_changed <> (NEW.placement_version IS DISTINCT FROM OLD.placement_version)
               OR receipt.completion_changed <> (NEW.completion_version IS DISTINCT FROM OLD.completion_version)
               {archive_transition}
               OR (NOT receipt.placement_changed AND (NEW.section_id IS DISTINCT FROM OLD.section_id OR NEW.registry_version_id IS DISTINCT FROM OLD.registry_version_id))
               OR (receipt.placement_changed AND NEW.placement_version <> NEW.dish_version)
               OR (NOT receipt.completion_changed AND (NEW.completed IS DISTINCT FROM OLD.completed OR NEW.completion_reason IS DISTINCT FROM OLD.completion_reason{archive_without_receipt}))
               OR (receipt.completion_changed AND NEW.completion_version <> NEW.dish_version)
            THEN RAISE EXCEPTION 'invalid DishState transition'; END IF;
          END IF;
          SELECT * INTO content FROM task_content_versions
           WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id AND content_version_id=NEW.current_content_version_id;
          IF content.content_version_id IS NULL THEN RAISE EXCEPTION 'DishState content missing'; END IF;
          IF TG_OP='INSERT' THEN
            SELECT creation_reason INTO generation_reason FROM authority_generations WHERE generation_id=NEW.generation_id;
            IF generation_reason IS DISTINCT FROM 'destructive_restore'
               AND (NEW.dish_version <> 1 OR NEW.placement_version <> 1 OR NEW.completion_version <> 1 OR content.created_dish_version <> 1)
            THEN RAISE EXCEPTION 'ordinary initial DishState must use version 1'; END IF;
            IF EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version IN (NEW.dish_version, NEW.placement_version, NEW.completion_version, content.created_dish_version)
                AND (r.content_changed IS DISTINCT FROM (r.dish_version=content.created_dish_version)
                  OR r.placement_changed IS DISTINCT FROM (r.dish_version=NEW.placement_version)
                  OR r.completion_changed IS DISTINCT FROM (r.dish_version=NEW.completion_version)
                  {initial_archive}))
            THEN RAISE EXCEPTION 'initial DishState receipt effects are not sparse-current'; END IF;
          END IF;
          IF TG_OP='UPDATE' AND receipt.content_changed AND content.created_dish_version <> NEW.dish_version
          THEN RAISE EXCEPTION 'DishState content occurrence is not current'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=content.created_dish_version AND r.content_changed
                AND ((r.source_route='import' AND content.creator_route='import' AND r.import_run_id=content.import_run_id)
                  OR (r.source_route='command_execution' AND content.creator_route='command_execution' AND r.command_execution_id=content.command_execution_id)))
          THEN RAISE EXCEPTION 'DishState content receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id AND r.dish_version=NEW.placement_version AND r.placement_changed)
          THEN RAISE EXCEPTION 'DishState placement receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=NEW.completion_version AND r.completion_changed
                AND ((r.source_route='import' AND NEW.completion_reason='imported')
                  OR (r.source_route='command_execution' AND NEW.completion_reason IN ('cooked','archive','reopen_planning'))))
          THEN RAISE EXCEPTION 'DishState completion receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM section_registry_entries e
              WHERE e.registry_version_id=NEW.registry_version_id AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id))
          THEN RAISE EXCEPTION 'DishState placement is absent from registry'; END IF;
          RETURN NEW;
        END; $$
    """)


def _replace_sqlite_guard(*, independent: bool) -> None:
    archive_transition = (
        " AND r.archive_changed=(NEW.archived_at IS NOT OLD.archived_at)"
        " AND (r.archive_changed=0 OR r.source_route='command_execution')"
        if independent
        else ""
    )
    archive_without_receipt = ""
    op.execute("DROP TRIGGER dish_states_validate_update")
    op.execute(f"""
        CREATE TRIGGER dish_states_validate_update BEFORE UPDATE ON dish_states WHEN
          NEW.dish_version <> OLD.dish_version + 1 OR
          NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
            WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
              AND r.dish_version=NEW.dish_version
              AND r.content_changed=(NEW.current_content_version_id IS NOT OLD.current_content_version_id)
              AND r.placement_changed=(NEW.placement_version<>OLD.placement_version)
              AND r.completion_changed=(NEW.completion_version<>OLD.completion_version)
              {archive_transition}) OR
          (NEW.placement_version=OLD.placement_version AND
            (NEW.section_id IS NOT OLD.section_id OR NEW.registry_version_id<>OLD.registry_version_id)) OR
          (NEW.placement_version<>OLD.placement_version AND NEW.placement_version<>NEW.dish_version) OR
          (NEW.completion_version=OLD.completion_version AND
            (NEW.completed<>OLD.completed OR NEW.completion_reason<>OLD.completion_reason{archive_without_receipt})) OR
          (NEW.completion_version<>OLD.completion_version AND NEW.completion_version<>NEW.dish_version) OR
          NOT EXISTS (SELECT 1 FROM task_content_versions cv JOIN dish_mutation_receipts r
            ON r.generation_id=cv.generation_id AND r.task_id=cv.task_id AND r.dish_version=cv.created_dish_version
            WHERE cv.generation_id=NEW.generation_id AND cv.task_id=NEW.task_id
              AND cv.content_version_id=NEW.current_content_version_id
              AND (NEW.current_content_version_id=OLD.current_content_version_id OR cv.created_dish_version=NEW.dish_version)
              AND ((r.source_route='import' AND cv.creator_route='import' AND r.import_run_id IS cv.import_run_id)
                OR (r.source_route='command_execution' AND cv.creator_route='command_execution'
                  AND r.command_execution_id IS cv.command_execution_id
                  AND EXISTS (SELECT 1 FROM command_executions ce WHERE ce.execution_id=cv.command_execution_id
                    AND ce.generation_id=cv.generation_id AND ce.task_id=cv.task_id
                    AND ce.contract_binding_id=cv.contract_binding_id)))) OR
          NOT EXISTS (SELECT 1 FROM section_registry_entries e
            WHERE e.registry_version_id=NEW.registry_version_id
              AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id)) OR
          NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
            WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
              AND r.dish_version=NEW.completion_version
              AND ((r.source_route='import' AND NEW.completion_reason='imported')
                OR (r.source_route='command_execution' AND
                  NEW.completion_reason IN ('cooked','archive','reopen_planning'))))
        BEGIN SELECT RAISE(ABORT, 'invalid DishState transition'); END
    """)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    _require_no_archived_rows(direction="upgrade")
    _add_archive_effect()
    if dialect == "postgresql":
        op.drop_constraint(
            "ck_dish_states_archived_at_matches_completion",
            "dish_states",
            type_="check",
        )
        _replace_postgresql_guard(independent=True)
    elif dialect == "sqlite":
        _replace_sqlite_guard(independent=True)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    _require_no_archived_rows(direction="downgrade")
    if dialect == "postgresql":
        _replace_postgresql_guard(independent=False)
        op.create_check_constraint(
            "ck_dish_states_archived_at_matches_completion",
            "dish_states",
            "(archived_at IS NOT NULL) = (completed AND completion_reason = 'archive')",
        )
    elif dialect == "sqlite":
        _replace_sqlite_guard(independent=False)
    _drop_archive_effect()
