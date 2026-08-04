"""Typed immutable projection-worker readiness probe evidence."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class WorkerProbeInventory(Base):
    __tablename__ = "worker_probe_inventories"

    inventory_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    inventory_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    required_probe_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inventory_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("inventory_version = 1", name="inventory_version_one"),
        CheckConstraint("required_probe_count > 0", name="required_probe_count_positive"),
        CheckConstraint("length(inventory_sha256) = 64", name="inventory_hash_length"),
        CheckConstraint("length(trim(inventory_contract_version)) > 0", name="inventory_contract_nonblank"),
        UniqueConstraint("candidate_id", "projection_epoch_id", name="uq_worker_probe_inventory_candidate_epoch"),
    )


class WorkerProbeRequirement(Base):
    __tablename__ = "worker_probe_requirements"

    requirement_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    inventory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("worker_probe_inventories.inventory_id", ondelete="RESTRICT"), nullable=False
    )
    probe_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    probe_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(probe_kind)) > 0", name="probe_kind_nonblank"),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("length(trim(probe_contract_version)) > 0", name="probe_contract_nonblank"),
        UniqueConstraint("inventory_id", "probe_kind", name="uq_worker_probe_requirement_kind"),
        UniqueConstraint("inventory_id", "ordinal", name="uq_worker_probe_requirement_ordinal"),
        UniqueConstraint("requirement_id", "inventory_id", "probe_kind", name="uq_worker_probe_requirement_exact"),
    )


class WorkerProbeEvidence(Base):
    __tablename__ = "worker_probe_evidence"

    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    readiness_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_worker_readiness.readiness_id", ondelete="RESTRICT"), nullable=False
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("worker_probe_requirements.requirement_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    inventory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("worker_probe_inventories.inventory_id", ondelete="RESTRICT"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    probe_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    worker_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    deployed_artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_artifact_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(probe_kind)) > 0", name="probe_kind_nonblank"),
        CheckConstraint("length(trim(execution_identity)) > 0", name="execution_identity_nonblank"),
        CheckConstraint("length(trim(worker_identity)) > 0", name="worker_identity_nonblank"),
        CheckConstraint("length(deployed_artifact_sha256) = 64", name="deployed_artifact_hash_length"),
        CheckConstraint("result IN ('pass','fail','error')", name="result_allowed"),
        CheckConstraint("length(trim(evidence_artifact_identity)) > 0", name="evidence_artifact_nonblank"),
        CheckConstraint("length(evidence_sha256) = 64", name="evidence_hash_length"),
        CheckConstraint("recorded_at >= observed_at", name="recording_after_observation"),
        UniqueConstraint("readiness_id", "probe_kind", name="uq_worker_probe_evidence_readiness_kind"),
        Index("ix_worker_probe_evidence_candidate_epoch", "candidate_id", "projection_epoch_id"),
    )


class WorkerReadinessCompletion(Base):
    __tablename__ = "worker_readiness_completions"

    completion_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    readiness_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_worker_readiness.readiness_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    inventory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("worker_probe_inventories.inventory_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    completion_state: Mapped[str] = mapped_column(String(16), nullable=False)
    required_probe_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    passed_probe_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completion_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("completion_state = 'complete'", name="completion_state_exact"),
        CheckConstraint("required_probe_count > 0 AND passed_probe_count = required_probe_count", name="exact_probe_counts"),
        CheckConstraint("length(completion_sha256) = 64", name="completion_hash_length"),
    )
