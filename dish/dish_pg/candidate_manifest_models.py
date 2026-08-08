"""Versioned release-candidate authority manifests and activation revalidation."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class ReleaseCandidateManifest(Base):
    __tablename__ = "release_candidate_manifests"

    manifest_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"),
        nullable=False,
    )
    manifest_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    canonical_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_import_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("source_import_batches.import_batch_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_import_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    shadow_baseline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("shadow_baselines.shadow_baseline_id", ondelete="RESTRICT"),
        nullable=False,
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"),
        nullable=False,
    )
    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    honest_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_reconciliation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("projection_reconciliation_runs.reconciliation_run_id", ondelete="RESTRICT"),
    )
    mapping_membership_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    import_completion_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    typed_import_linkage_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reconciliation_evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    readiness_inventory_sha256: Mapped[str | None] = mapped_column(String(64))
    readiness_completion_sha256: Mapped[str | None] = mapped_column(String(64))
    builder_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("manifest_version IN (2, 3)", name="manifest_version_supported"),
        CheckConstraint(
            "length(canonical_fingerprint) = 64", name="fingerprint_hash_length"
        ),
        CheckConstraint(
            "length(mapping_membership_sha256) = 64 AND "
            "length(import_completion_sha256) = 64 AND "
            "length(typed_import_linkage_sha256) = 64 AND "
            "length(reconciliation_evidence_sha256) = 64 AND "
            "((manifest_version = 2 AND approval_reconciliation_run_id IS NULL "
            "AND length(readiness_inventory_sha256) = 64 "
            "AND length(readiness_completion_sha256) = 64) OR "
            "(manifest_version = 3 AND approval_reconciliation_run_id IS NOT NULL "
            "AND readiness_inventory_sha256 IS NULL "
            "AND readiness_completion_sha256 IS NULL))",
            name="component_hash_lengths",
        ),
        CheckConstraint(
            "length(trim(builder_contract_version)) > 0",
            name="builder_contract_nonblank",
        ),
        UniqueConstraint(
            "candidate_id", "manifest_version", name="uq_candidate_manifest_version"
        ),
        UniqueConstraint(
            "candidate_id",
            "canonical_fingerprint",
            name="uq_candidate_manifest_fingerprint",
        ),
        UniqueConstraint(
            "manifest_id",
            "candidate_id",
            "manifest_version",
            "canonical_fingerprint",
            name="uq_candidate_manifest_exact_identity",
        ),
    )


class CutoverApprovalManifestBinding(Base):
    __tablename__ = "cutover_approval_manifest_bindings"

    binding_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    manifest_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    manifest_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    canonical_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["approval_id", "candidate_id"],
            ["cutover_approvals.approval_id", "cutover_approvals.candidate_id"],
            name="fk_approval_manifest_binding_exact_approval",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["manifest_id", "candidate_id", "manifest_version", "canonical_fingerprint"],
            [
                "release_candidate_manifests.manifest_id",
                "release_candidate_manifests.candidate_id",
                "release_candidate_manifests.manifest_version",
                "release_candidate_manifests.canonical_fingerprint",
            ],
            name="fk_approval_manifest_binding_exact_manifest",
            ondelete="RESTRICT",
        ),
        CheckConstraint("manifest_version IN (2, 3)", name="manifest_version_supported"),
        CheckConstraint(
            "length(canonical_fingerprint) = 64", name="fingerprint_hash_length"
        ),
    )


class CandidateManifestRevalidation(Base):
    __tablename__ = "candidate_manifest_revalidations"

    revalidation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    manifest_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    manifest_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approved_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_mapping_membership_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    observed_import_completion_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    observed_typed_import_linkage_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    observed_reconciliation_evidence_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    observed_readiness_inventory_sha256: Mapped[str | None] = mapped_column(String(64))
    observed_readiness_completion_sha256: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    revalidated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["manifest_id", "candidate_id", "manifest_version", "approved_fingerprint"],
            [
                "release_candidate_manifests.manifest_id",
                "release_candidate_manifests.candidate_id",
                "release_candidate_manifests.manifest_version",
                "release_candidate_manifests.canonical_fingerprint",
            ],
            name="fk_candidate_manifest_revalidation_exact_manifest",
            ondelete="RESTRICT",
        ),
        CheckConstraint("manifest_version IN (2, 3)", name="manifest_version_supported"),
        CheckConstraint(
            "length(approved_fingerprint) = 64 AND "
            "length(observed_fingerprint) = 64",
            name="fingerprint_hash_lengths",
        ),
        CheckConstraint(
            "length(observed_mapping_membership_sha256) = 64 AND "
            "length(observed_import_completion_sha256) = 64 AND "
            "length(observed_typed_import_linkage_sha256) = 64 AND "
            "length(observed_reconciliation_evidence_sha256) = 64 AND "
            "((manifest_version = 2 AND length(observed_readiness_inventory_sha256) = 64 "
            "AND length(observed_readiness_completion_sha256) = 64) OR "
            "(manifest_version = 3 AND observed_readiness_inventory_sha256 IS NULL "
            "AND observed_readiness_completion_sha256 IS NULL))",
            name="observed_component_hash_lengths",
        ),
        CheckConstraint("result IN ('matched','stale')", name="result_allowed"),
        CheckConstraint(
            "(result = 'matched' AND approved_fingerprint = observed_fingerprint) OR "
            "(result = 'stale' AND approved_fingerprint <> observed_fingerprint)",
            name="result_matches_fingerprint",
        ),
        UniqueConstraint(
            "candidate_id",
            "observed_fingerprint",
            "revalidated_at",
            name="uq_candidate_revalidation_observation_time",
        ),
        Index(
            "ix_candidate_manifest_revalidations_latest",
            "candidate_id",
            "revalidated_at",
        ),
    )
