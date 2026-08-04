"""Immutable request-identity tombstones imported from legacy authority."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class LegacyRequestTombstone(Base):
    __tablename__ = "legacy_request_tombstones"

    tombstone_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    source_authority: Mapped[str] = mapped_column(String(64), nullable=False)
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"), nullable=False
    )
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("source_import_batches.import_batch_id", ondelete="RESTRICT")
    )
    source_identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_legacy_request_tombstones_import_run", "import_run_id"),
        CheckConstraint("length(trim(source_authority)) > 0", name="source_authority_nonblank"),
        CheckConstraint("length(source_identity_sha256) = 64", name="source_identity_hash_length"),
    )
