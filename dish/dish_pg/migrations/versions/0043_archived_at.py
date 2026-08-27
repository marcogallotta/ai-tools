"""Persist PostgreSQL-native Dish archive state for frontend reads.

Revision ID: 0043_archived_at
Revises: 0042_scalar_dish_state
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0043_archived_at"
down_revision = "0042_scalar_dish_state"
branch_labels = None
depends_on = None


def _replace_postgresql_guard(*, include_archived_at: bool) -> None:
    archived_guard = (
        " OR NEW.archived_at IS DISTINCT FROM OLD.archived_at"
        if include_archived_at
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION dish_validate_scalar_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE receipt dish_mutation_receipts%ROWTYPE;
        DECLARE content task_content_versions%ROWTYPE;
        DECLARE generation_reason text;
        BEGIN
          SELECT * INTO receipt FROM dish_mutation_receipts
           WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id
             AND dish_version=NEW.dish_version;
          IF receipt.dish_version IS NULL THEN RAISE EXCEPTION 'DishState receipt missing'; END IF;
          IF TG_OP='UPDATE' THEN
            IF NEW.dish_version <> OLD.dish_version + 1
               OR receipt.content_changed <> (NEW.current_content_version_id IS DISTINCT FROM OLD.current_content_version_id)
               OR receipt.placement_changed <> (NEW.placement_version IS DISTINCT FROM OLD.placement_version)
               OR receipt.completion_changed <> (NEW.completion_version IS DISTINCT FROM OLD.completion_version)
               OR (NOT receipt.placement_changed AND (NEW.section_id IS DISTINCT FROM OLD.section_id
                   OR NEW.registry_version_id IS DISTINCT FROM OLD.registry_version_id))
               OR (receipt.placement_changed AND NEW.placement_version <> NEW.dish_version)
               OR (NOT receipt.completion_changed AND (NEW.completed IS DISTINCT FROM OLD.completed
                   OR NEW.completion_reason IS DISTINCT FROM OLD.completion_reason
                   {archived_guard}))
               OR (receipt.completion_changed AND NEW.completion_version <> NEW.dish_version)
            THEN RAISE EXCEPTION 'invalid DishState transition'; END IF;
          END IF;
          SELECT * INTO content FROM task_content_versions
           WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id
             AND content_version_id=NEW.current_content_version_id;
          IF content.content_version_id IS NULL THEN RAISE EXCEPTION 'DishState content missing'; END IF;
          IF TG_OP='INSERT' THEN
            SELECT creation_reason INTO generation_reason FROM authority_generations
             WHERE generation_id=NEW.generation_id;
            IF generation_reason IS DISTINCT FROM 'destructive_restore'
               AND (NEW.dish_version <> 1 OR NEW.placement_version <> 1
                 OR NEW.completion_version <> 1 OR content.created_dish_version <> 1)
            THEN RAISE EXCEPTION 'ordinary initial DishState must use version 1'; END IF;
            IF EXISTS (
              SELECT 1 FROM dish_mutation_receipts r
               WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                 AND r.dish_version IN (
                   NEW.dish_version, NEW.placement_version,
                   NEW.completion_version, content.created_dish_version
                 )
                 AND (r.content_changed IS DISTINCT FROM
                        (r.dish_version=content.created_dish_version)
                   OR r.placement_changed IS DISTINCT FROM
                        (r.dish_version=NEW.placement_version)
                   OR r.completion_changed IS DISTINCT FROM
                        (r.dish_version=NEW.completion_version))
            ) THEN RAISE EXCEPTION 'initial DishState receipt effects are not sparse-current'; END IF;
          END IF;
          IF TG_OP='UPDATE' AND receipt.content_changed
             AND content.created_dish_version <> NEW.dish_version
          THEN RAISE EXCEPTION 'DishState content occurrence is not current'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=content.created_dish_version AND r.content_changed
                AND ((r.source_route='import' AND content.creator_route='import' AND r.import_run_id=content.import_run_id)
                  OR (r.source_route='command_execution' AND content.creator_route='command_execution'
                    AND r.command_execution_id=content.command_execution_id)))
          THEN RAISE EXCEPTION 'DishState content receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=NEW.placement_version AND r.placement_changed)
          THEN RAISE EXCEPTION 'DishState placement receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=NEW.completion_version AND r.completion_changed
                AND ((r.source_route='import' AND NEW.completion_reason='imported')
                  OR (r.source_route='command_execution' AND NEW.completion_reason IN ('cooked','archive','reopen_planning'))))
          THEN RAISE EXCEPTION 'DishState completion receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM section_registry_entries e
              WHERE e.registry_version_id=NEW.registry_version_id
                AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id))
          THEN RAISE EXCEPTION 'DishState placement is absent from registry'; END IF;
          RETURN NEW;
        END; $$
        """
    )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.add_column(
        "dish_states",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE dish_states SET archived_at=updated_at "
        "WHERE completed AND completion_reason='archive'"
    )
    if dialect == "postgresql":
        op.execute(
            """
            WITH RECURSIVE archived_lineage(generation_id, task_id, archived_at) AS (
              SELECT generation_id, task_id, archived_at
                FROM dish_states
               WHERE archived_at IS NOT NULL
              UNION
              SELECT child.generation_id, child.task_id, parent.archived_at
                FROM archived_lineage parent
                JOIN authority_generations child_generation
                  ON child_generation.predecessor_generation_id=parent.generation_id
                JOIN dish_states child
                  ON child.generation_id=child_generation.generation_id
                 AND child.task_id=parent.task_id
               WHERE child.completed
                 AND child.completion_reason='imported'
                 AND child.archived_at IS NULL
            )
            UPDATE dish_states target
               SET archived_at=archived_lineage.archived_at
              FROM archived_lineage
             WHERE target.generation_id=archived_lineage.generation_id
               AND target.task_id=archived_lineage.task_id
               AND target.archived_at IS NULL
            """
        )
    if dialect == "postgresql":
        op.create_check_constraint(
            "ck_dish_states_archived_at_matches_completion",
            "dish_states",
            "(archived_at IS NULL OR (completed AND completion_reason IN ('archive','imported'))) "
            "AND (NOT completed OR completion_reason <> 'archive' OR archived_at IS NOT NULL)",
        )
    op.create_index(
        "ix_dish_states_archive",
        "dish_states",
        ["generation_id", "archived_at", "task_id"],
    )
    if dialect == "postgresql":
        _replace_postgresql_guard(include_archived_at=True)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _replace_postgresql_guard(include_archived_at=False)
    op.drop_index("ix_dish_states_archive", table_name="dish_states")
    if dialect == "postgresql":
        op.drop_constraint(
            "ck_dish_states_archived_at_matches_completion",
            "dish_states",
            type_="check",
        )
    op.drop_column("dish_states", "archived_at")
