"""Stage 6 rehearsal, release-candidate, cutover, and admission authority."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from .models import Base


class ReleaseCandidate(Base):
    __tablename__ = "release_candidates"

    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    source_import_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("source_import_batches.import_batch_id", ondelete="RESTRICT"), nullable=False
    )
    shadow_baseline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shadow_baselines.shadow_baseline_id", ondelete="RESTRICT"), nullable=False
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    source_release: Mapped[str] = mapped_column(String(128), nullable=False)
    source_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    ledger_through_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_head: Mapped[str] = mapped_column(String(64), nullable=False)
    dish_release: Mapped[str] = mapped_column(String(128), nullable=False)
    honest_release: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_release: Mapped[str] = mapped_column(String(128), nullable=False)
    openapi_release: Mapped[str] = mapped_column(String(128), nullable=False)
    routing_release: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="assembling")
    candidate_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    validation_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('assembling','validated','approved','activated','aborted')",
            name="status_allowed",
        ),
        CheckConstraint("candidate_revision > 0", name="positive_revision"),
        CheckConstraint(
            "validation_bundle_sha256 IS NULL OR length(validation_bundle_sha256) = 64",
            name="validation_hash_length",
        ),
        CheckConstraint(
            "(status = 'assembling' AND validated_at IS NULL AND approved_at IS NULL "
            "AND terminal_at IS NULL AND validation_bundle_sha256 IS NULL) OR "
            "(status = 'validated' AND validated_at IS NOT NULL AND approved_at IS NULL "
            "AND terminal_at IS NULL AND validation_bundle_sha256 IS NOT NULL) OR "
            "(status = 'approved' AND validated_at IS NOT NULL AND approved_at IS NOT NULL "
            "AND terminal_at IS NULL AND validation_bundle_sha256 IS NOT NULL) OR "
            "(status = 'activated' AND validated_at IS NOT NULL AND approved_at IS NOT NULL "
            "AND terminal_at IS NOT NULL AND validation_bundle_sha256 IS NOT NULL) OR "
            "(status = 'aborted' AND terminal_at IS NOT NULL)",
            name="status_timestamps_consistent",
        ),
        UniqueConstraint(
            "generation_id", "source_import_batch_id", "source_commit", name="uq_release_candidate_source"
        ),
        UniqueConstraint(
            "candidate_id", "generation_id", name="uq_release_candidate_generation_identity"
        ),
        Index(
            "uq_release_candidate_one_live_generation",
            "generation_id",
            unique=True,
            postgresql_where=text("status IN ('assembling','validated','approved')"),
            sqlite_where=text("status IN ('assembling','validated','approved')"),
        ),
    )


class ReleaseEvidenceItem(Base):
    __tablename__ = "release_evidence_items"

    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(category)) > 0", name="category_nonblank"),
        CheckConstraint("length(trim(evidence_key)) > 0", name="key_nonblank"),
        CheckConstraint("evidence_revision > 0", name="positive_revision"),
        CheckConstraint("outcome IN ('pass','fail','blocked','info')", name="outcome_allowed"),
        CheckConstraint("length(payload_sha256) = 64", name="payload_hash_length"),
        UniqueConstraint(
            "candidate_id", "category", "evidence_key", "evidence_revision",
            name="uq_release_evidence_revision",
        ),
    )


class RehearsalRun(Base):
    __tablename__ = "rehearsal_runs"

    rehearsal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False
    )
    rehearsal_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    environment_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    run_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    report_sha256: Mapped[str | None] = mapped_column(String(64))
    measured_rpo_seconds: Mapped[float | None] = mapped_column(Float)
    measured_rto_seconds: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "rehearsal_kind IN ('full','activation','restore','fault_injection')",
            name="kind_allowed",
        ),
        CheckConstraint("status IN ('running','passed','failed')", name="status_allowed"),
        CheckConstraint("run_revision > 0", name="positive_revision"),
        CheckConstraint("length(source_manifest_sha256) = 64", name="source_hash_length"),
        CheckConstraint(
            "report_sha256 IS NULL OR length(report_sha256) = 64", name="report_hash_length"
        ),
        CheckConstraint(
            "measured_rpo_seconds IS NULL OR measured_rpo_seconds >= 0", name="rpo_nonnegative"
        ),
        CheckConstraint(
            "measured_rto_seconds IS NULL OR measured_rto_seconds >= 0", name="rto_nonnegative"
        ),
        CheckConstraint(
            "(status = 'running' AND report IS NULL AND report_sha256 IS NULL AND completed_at IS NULL) OR "
            "(status IN ('passed','failed') AND report IS NOT NULL AND report_sha256 IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="terminal_report_consistent",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="completion_not_before_start",
        ),
        UniqueConstraint(
            "candidate_id", "rehearsal_kind", "environment_identity", "source_manifest_sha256",
            name="uq_rehearsal_identity",
        ),
    )


class RehearsalCheckpoint(Base):
    __tablename__ = "rehearsal_checkpoints"

    checkpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    rehearsal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rehearsal_runs.rehearsal_id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("sequence > 0", name="positive_sequence"),
        CheckConstraint("length(trim(checkpoint_kind)) > 0", name="kind_nonblank"),
        CheckConstraint("length(payload_sha256) = 64", name="payload_hash_length"),
        UniqueConstraint("rehearsal_id", "sequence", name="uq_rehearsal_checkpoint_sequence"),
    )


class LegacyWriterFence(Base):
    __tablename__ = "legacy_writer_fences"

    fence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False
    )
    target_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    mechanism: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    fence_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    proof_sha256: Mapped[str | None] = mapped_column(String(64))
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact_observation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    artifact_verification_result: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_observation_id", "fence_id", "candidate_id", "artifact_verification_result"],
            [
                "writer_fence_artifact_observations.observation_id",
                "writer_fence_artifact_observations.fence_id",
                "writer_fence_artifact_observations.candidate_id",
                "writer_fence_artifact_observations.verification_result",
            ],
            ondelete="RESTRICT",
            name="fk_writer_fence_exact_artifact_observation",
        ),
        CheckConstraint("state IN ('prepared','engaged','verified','released')", name="state_allowed"),
        CheckConstraint("fence_revision > 0", name="positive_revision"),
        CheckConstraint("length(manifest_sha256) = 64", name="manifest_hash_length"),
        CheckConstraint("proof_sha256 IS NULL OR length(proof_sha256) = 64", name="proof_hash_length"),
        CheckConstraint(
            "(state = 'prepared' AND engaged_at IS NULL AND verified_at IS NULL AND released_at IS NULL "
            "AND proof_sha256 IS NULL AND artifact_observation_id IS NULL "
            "AND artifact_verification_result IS NULL) OR "
            "(state = 'engaged' AND engaged_at IS NOT NULL AND verified_at IS NULL AND released_at IS NULL "
            "AND proof_sha256 IS NULL AND artifact_observation_id IS NOT NULL "
            "AND artifact_verification_result = 'matched') OR "
            "(state = 'verified' AND engaged_at IS NOT NULL AND verified_at IS NOT NULL "
            "AND released_at IS NULL AND proof_sha256 IS NOT NULL AND artifact_observation_id IS NOT NULL "
            "AND artifact_verification_result = 'matched') OR "
            "(state = 'released' AND engaged_at IS NOT NULL AND released_at IS NOT NULL "
            "AND artifact_observation_id IS NOT NULL AND artifact_verification_result = 'matched')",
            name="state_payload_consistent",
        ),
        UniqueConstraint("candidate_id", "target_identity", name="uq_fence_candidate_target"),
        UniqueConstraint("fence_id", "candidate_id", name="uq_writer_fence_candidate_identity"),
    )


class EvidenceBundle(Base):
    __tablename__ = "release_evidence_bundles"

    bundle_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False
    )
    bundle_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    bundle_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("bundle_kind IN ('release_candidate','cutover_final')", name="kind_allowed"),
        CheckConstraint("bundle_revision > 0", name="positive_revision"),
        CheckConstraint("length(manifest_sha256) = 64", name="manifest_hash_length"),
        UniqueConstraint("candidate_id", "bundle_kind", "bundle_revision", name="uq_bundle_revision"),
        UniqueConstraint("candidate_id", "manifest_sha256", name="uq_bundle_manifest"),
    )


class CutoverApproval(Base):
    __tablename__ = "cutover_approvals"

    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    evidence_bundle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_evidence_bundles.bundle_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    approver: Mapped[str] = mapped_column(String(256), nullable=False)
    approval_statement: Mapped[str] = mapped_column(Text, nullable=False)
    approval_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approval_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(approver)) > 0", name="approver_nonblank"),
        CheckConstraint("length(trim(approval_statement)) > 0", name="statement_nonblank"),
        CheckConstraint("length(approval_sha256) = 64", name="approval_hash_length"),
    )


class CutoverRun(Base):
    __tablename__ = "cutover_runs"

    cutover_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared")
    state_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "cutover_run_id", "candidate_id", name="uq_cutover_run_candidate_identity"
        ),
        CheckConstraint(
            "state IN ('prepared','fenced','activated','rollback_burned','admission_open',"
            "'first_admission_verified','completed','aborted')",
            name="state_allowed",
        ),
        CheckConstraint("state_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state IN ('completed','aborted') AND terminal_at IS NOT NULL) OR "
            "(state NOT IN ('completed','aborted') AND terminal_at IS NULL)",
            name="terminal_time_consistent",
        ),
    )


class CutoverCheckpoint(Base):
    __tablename__ = "cutover_checkpoints"

    checkpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cutover_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cutover_runs.cutover_run_id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("sequence > 0", name="positive_sequence"),
        CheckConstraint("length(trim(checkpoint_kind)) > 0", name="kind_nonblank"),
        CheckConstraint("length(payload_sha256) = 64", name="payload_hash_length"),
        UniqueConstraint("cutover_run_id", "sequence", name="uq_cutover_checkpoint_sequence"),
        UniqueConstraint("cutover_run_id", "checkpoint_kind", name="uq_cutover_checkpoint_kind"),
    )


class FinalAsanaClosure(Base):
    __tablename__ = "final_asana_closures"

    closure_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False
    )
    capture_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_high_water: Mapped[str] = mapped_column(String(256), nullable=False)
    watcher_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    interval_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_through_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    closure_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(capture_manifest_sha256) = 64", name="capture_hash_length"),
        CheckConstraint("length(closure_sha256) = 64", name="closure_hash_length"),
        CheckConstraint("length(trim(observation_high_water)) > 0", name="high_water_nonblank"),
        CheckConstraint("length(trim(watcher_identity)) > 0", name="watcher_nonblank"),
        CheckConstraint("closed_through_at >= interval_started_at", name="interval_ordered"),
        UniqueConstraint("candidate_id", "closure_sha256", name="uq_final_asana_closure_identity"),
    )


class FinalAsanaClosureInvalidation(Base):
    __tablename__ = "final_asana_closure_invalidations"

    invalidation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    closure_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("final_asana_closures.closure_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    change_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    change_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    invalidation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(change_identity)) > 0", name="change_identity_nonblank"),
        CheckConstraint("length(trim(change_kind)) > 0", name="change_kind_nonblank"),
        CheckConstraint("length(invalidation_sha256) = 64", name="invalidation_hash_length"),
    )


class CutoverRecertification(Base):
    __tablename__ = "cutover_recertifications"

    recertification_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cutover_approvals.approval_id", ondelete="RESTRICT"), nullable=False
    )
    closure_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("final_asana_closures.closure_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    recertification_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approver: Mapped[str] = mapped_column(String(256), nullable=False)
    recertification_statement: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recertification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recertified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("recertification_revision > 0", name="positive_revision"),
        CheckConstraint("length(trim(approver)) > 0", name="approver_nonblank"),
        CheckConstraint("length(trim(recertification_statement)) > 0", name="statement_nonblank"),
        CheckConstraint("length(recertification_sha256) = 64", name="recertification_hash_length"),
        UniqueConstraint("candidate_id", "recertification_revision", name="uq_recertification_revision"),
    )


class RuntimeReleaseAttestation(Base):
    __tablename__ = "runtime_release_attestations"

    attestation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    service_artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_worker_artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    route_probe_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(service_artifact_sha256) = 64", name="service_hash_length"),
        CheckConstraint("length(projection_worker_artifact_sha256) = 64", name="worker_hash_length"),
        CheckConstraint("length(route_probe_sha256) = 64", name="route_hash_length"),
        CheckConstraint("length(attestation_sha256) = 64", name="attestation_hash_length"),
    )


class ProjectionWorkerReadiness(Base):
    __tablename__ = "projection_worker_readiness"

    readiness_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_reconciliation_runs.reconciliation_run_id", ondelete="RESTRICT"), nullable=False
    )
    worker_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    worker_release: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    readiness_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(worker_identity)) > 0", name="worker_identity_nonblank"),
        CheckConstraint("length(trim(worker_release)) > 0", name="worker_release_nonblank"),
        CheckConstraint("length(readiness_sha256) = 64", name="readiness_hash_length"),
    )


class FirstAdmissionPlan(Base):
    __tablename__ = "first_admission_plans"

    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cutover_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cutover_runs.cutover_run_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    command_name: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT")
    )
    expected_projection_events: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(command_name)) > 0", name="command_nonblank"),
        CheckConstraint("expected_projection_events >= 0", name="projection_count_nonnegative"),
        CheckConstraint("length(plan_sha256) = 64", name="plan_hash_length"),
        UniqueConstraint(
            "plan_id",
            "cutover_run_id",
            "request_id",
            "command_name",
            name="uq_first_admission_plan_exact_request",
        ),
    )


class MutationAdmissionControl(Base):
    __tablename__ = "mutation_admission_controls"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), primary_key=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("release_candidates.candidate_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="closed")
    control_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("state IN ('closed','open')", name="state_allowed"),
        CheckConstraint("control_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'closed' AND opened_at IS NULL) OR (state = 'open' AND opened_at IS NOT NULL)",
            name="open_time_consistent",
        ),
    )


STAGE6_TABLE_NAMES = (
    "release_candidates",
    "release_evidence_items",
    "rehearsal_runs",
    "rehearsal_checkpoints",
    "legacy_writer_fences",
    "release_evidence_bundles",
    "cutover_approvals",
    "cutover_runs",
    "cutover_checkpoints",
    "mutation_admission_controls",
)

STAGE6_IMMUTABLE_TABLE_NAMES = (
    "release_evidence_items",
    "rehearsal_checkpoints",
    "release_evidence_bundles",
    "cutover_approvals",
    "cutover_checkpoints",
)

STAGE7_TABLE_NAMES = (
    "final_asana_closures",
    "final_asana_closure_invalidations",
    "cutover_recertifications",
)

STAGE7_IMMUTABLE_TABLE_NAMES = STAGE7_TABLE_NAMES

STAGE8_TABLE_NAMES = (
    "runtime_release_attestations",
    "projection_worker_readiness",
    "first_admission_plans",
)

STAGE8_IMMUTABLE_TABLE_NAMES = STAGE8_TABLE_NAMES


def _install_sqlite_immutability_triggers() -> None:
    for table_name in STAGE6_IMMUTABLE_TABLE_NAMES + STAGE7_IMMUTABLE_TABLE_NAMES + STAGE8_IMMUTABLE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_immutable_update BEFORE UPDATE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable Stage 6 evidence row'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_immutable_delete BEFORE DELETE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable Stage 6 evidence row'); END"
            ).execute_if(dialect="sqlite"),
        )


def _install_sqlite_admission_guard() -> None:
    requests = Base.metadata.tables["service_requests"]
    event.listen(
        requests,
        "after_create",
        DDL(
            "CREATE TRIGGER service_requests_stage6_admission_guard "
            "BEFORE INSERT ON service_requests "
            "WHEN EXISTS (SELECT 1 FROM release_candidates rc "
            "WHERE rc.generation_id = NEW.generation_id) "
            "AND NOT EXISTS (SELECT 1 FROM mutation_admission_controls mac "
            "WHERE mac.generation_id = NEW.generation_id AND mac.state = 'open') "
            "BEGIN SELECT RAISE(ABORT, 'PostgreSQL mutation admission is closed'); END"
        ).execute_if(dialect="sqlite"),
    )


_install_sqlite_immutability_triggers()
_install_sqlite_admission_guard()
