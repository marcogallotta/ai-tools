"""Verified filesystem-artifact identity bound to a legacy writer fence."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class WriterFenceArtifactObservation(Base):
    __tablename__ = "writer_fence_artifact_observations"

    observation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    fence_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    artifact_generation_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filesystem_device: Mapped[int] = mapped_column(BigInteger, nullable=False)
    filesystem_inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    regular_file: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_result: Mapped[str] = mapped_column(String(16), nullable=False)
    observation_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fence_id", "candidate_id"],
            ["legacy_writer_fences.fence_id", "legacy_writer_fences.candidate_id"],
            ondelete="RESTRICT",
            name="fk_writer_fence_artifact_observation_exact_fence",
        ),
        CheckConstraint("length(trim(artifact_generation_identity)) > 0", name="generation_identity_nonblank"),
        CheckConstraint("canonical_path LIKE '/%' AND canonical_path NOT LIKE '%/../%'", name="canonical_path_absolute"),
        CheckConstraint("length(content_sha256) = 64", name="content_hash_length"),
        CheckConstraint("filesystem_device >= 0 AND filesystem_inode > 0", name="filesystem_identity_positive"),
        CheckConstraint("file_type IN ('regular')", name="file_type_allowed"),
        CheckConstraint("regular_file", name="regular_file_required"),
        CheckConstraint("verification_result IN ('matched','mismatched','unverifiable')", name="verification_result_allowed"),
        CheckConstraint("length(trim(observation_contract_version)) > 0", name="contract_version_nonblank"),
        CheckConstraint("recorded_at >= observed_at", name="recording_after_observation"),
        CheckConstraint("length(evidence_sha256) = 64", name="evidence_hash_length"),
        UniqueConstraint(
            "observation_id", "fence_id", "candidate_id", "verification_result",
            name="uq_writer_fence_artifact_observation_binding",
        ),
        UniqueConstraint(
            "candidate_id", "canonical_path", "filesystem_device", "filesystem_inode", "content_sha256",
            name="uq_writer_fence_artifact_identity",
        ),
    )
