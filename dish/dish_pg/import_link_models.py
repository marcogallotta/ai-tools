"""Typed links from imported source evidence to native PostgreSQL authority."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class SourceImportNativeLink(Base):
    __tablename__ = "source_import_native_links"

    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("source_import_entity_evidence.evidence_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("source_import_batches.import_batch_id", ondelete="RESTRICT"), nullable=False
    )
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"), nullable=False
    )
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("governed_projects.project_id", ondelete="RESTRICT")
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT")
    )
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT")
    )
    request_tombstone_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("legacy_request_tombstones.tombstone_id", ondelete="RESTRICT")
    )
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(entity_kind='project' AND project_id IS NOT NULL AND section_id IS NULL AND task_id IS NULL AND content_version_id IS NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='section' AND project_id IS NULL AND section_id IS NOT NULL AND task_id IS NULL AND content_version_id IS NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='task' AND project_id IS NULL AND section_id IS NULL AND task_id IS NOT NULL AND content_version_id IS NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='content' AND project_id IS NULL AND section_id IS NULL AND task_id IS NULL AND content_version_id IS NOT NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='request_tombstone' AND project_id IS NULL AND section_id IS NULL AND task_id IS NULL AND content_version_id IS NULL AND request_tombstone_id IS NOT NULL)",
            name="exact_typed_target",
        ),
        UniqueConstraint("import_batch_id", "project_id", name="uq_import_native_project"),
        UniqueConstraint("import_batch_id", "section_id", name="uq_import_native_section"),
        UniqueConstraint("import_batch_id", "task_id", name="uq_import_native_task"),
        UniqueConstraint("import_batch_id", "content_version_id", name="uq_import_native_content"),
        UniqueConstraint("import_batch_id", "request_tombstone_id", name="uq_import_native_tombstone"),
        Index("ix_import_native_links_run_kind", "import_run_id", "entity_kind"),
    )
