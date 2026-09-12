"""Persistence support for catalog-reference-only native Section lifecycle rebinding."""
from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from . import models


class SectionCatalogRebindReceipt(models.Base):
    """Audit one Dish catalog-reference rebind without claiming a Dish move."""

    __tablename__ = "section_catalog_rebind_receipts"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    predecessor_catalog_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    section_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    command_execution_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["command_execution_id", "generation_id"],
            ["command_executions.execution_id", "command_executions.generation_id"],
            ondelete="RESTRICT",
            name="fk_section_catalog_rebind_exact_execution",
        ),
        ForeignKeyConstraint(
            ["predecessor_catalog_version_id", "section_id"],
            ["section_catalog_entries.catalog_version_id", "section_catalog_entries.section_id"],
            ondelete="RESTRICT",
            name="fk_section_catalog_rebind_predecessor_entry",
        ),
        ForeignKeyConstraint(
            ["catalog_version_id", "section_id"],
            ["section_catalog_entries.catalog_version_id", "section_catalog_entries.section_id"],
            ondelete="RESTRICT",
            name="fk_section_catalog_rebind_successor_entry",
        ),
        CheckConstraint(
            "catalog_version_id <> predecessor_catalog_version_id",
            name="catalog_changes",
        ),
        UniqueConstraint(
            "command_execution_id",
            "task_id",
            name="uq_section_catalog_rebind_execution_task",
        ),
    )


# Keep the established core-model namespace available to command/test code while
# this bounded lifecycle model remains owned by its dedicated module.
models.SectionCatalogRebindReceipt = SectionCatalogRebindReceipt


def _sqlite_rebind_match() -> str:
    return (
        "(NEW.generation_id=OLD.generation_id AND NEW.task_id=OLD.task_id "
        "AND NEW.catalog_version_id IS NOT OLD.catalog_version_id "
        "AND NEW.current_content_version_id IS OLD.current_content_version_id "
        "AND NEW.section_id IS OLD.section_id "
        "AND NEW.registry_version_id=OLD.registry_version_id "
        "AND NEW.completed=OLD.completed "
        "AND NEW.completion_reason=OLD.completion_reason "
        "AND NEW.archived_at IS OLD.archived_at "
        "AND NEW.dish_version=OLD.dish_version "
        "AND NEW.placement_version=OLD.placement_version "
        "AND NEW.completion_version=OLD.completion_version "
        "AND EXISTS (SELECT 1 FROM section_catalog_rebind_receipts cr "
        "JOIN command_executions ce ON ce.execution_id=cr.command_execution_id "
        "AND ce.generation_id=cr.generation_id "
        "WHERE cr.generation_id=NEW.generation_id AND cr.task_id=NEW.task_id "
        "AND cr.predecessor_catalog_version_id=OLD.catalog_version_id "
        "AND cr.catalog_version_id=NEW.catalog_version_id "
        "AND cr.section_id=NEW.section_id AND ce.status='claimed' "
        "AND ce.command_name IN ('create-section','rename-section','retire-section')))"
    )


def _install_sqlite_rebind_guard(_target, connection, **_kw) -> None:
    if connection.dialect.name != "sqlite":
        return
    name = "dish_states_validate_update"
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).scalar_one_or_none()
    if sql is None:
        raise RuntimeError(f"{name} is required for native Section lifecycle support")
    match = re.search(
        r"(BEFORE\s+UPDATE\s+ON\s+dish_states\s+WHEN\s*)(.*?)(\s*BEGIN\s+SELECT\s+RAISE\(ABORT,\s*'invalid DishState transition'\);\s*END)",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError(f"{name} has an unexpected scalar guard shape")
    wrapper = f"NOT {_sqlite_rebind_match()} AND ("
    predicate = match.group(2).strip()
    if predicate.startswith(wrapper) and predicate.endswith(")"):
        return
    replacement = sql[: match.start(2)] + wrapper + predicate + ")" + sql[match.end(2) :]
    connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    connection.exec_driver_sql(replacement)


event.listen(models.DishState.__table__, "after_create", _install_sqlite_rebind_guard)
