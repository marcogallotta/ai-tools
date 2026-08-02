"""Stage 6 release-candidate validation, evidence, and cutover control.

All methods participate in the caller-owned SQLAlchemy transaction.  This module
performs no network I/O and never fabricates environment evidence: production
snapshots, restore measurements, fence probes, and Marco approval are supplied
as exact, digest-bound inputs.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from . import stage6_models as rel
from .command_contract import definition_for
from .command_effects import expected_projection_count

ALEMBIC_HEAD = "0008_fail_closed_admission_outbox"

REQUIRED_EVIDENCE = (
    ("authority_coverage", "current_to_target"),
    ("command_semantic_delta", "retained_commands"),
    ("characterization", "frozen_current_behavior"),
    ("production_change_ledger", "source_commit_closure"),
    ("fault_injection", "crash_boundaries"),
    ("contention", "same_task_and_independent_tasks"),
    ("backup_restore", "restore_rehearsal"),
    ("create_correlation", "lost_response_safety"),
    ("protocol_coherence", "service_openapi_routing"),
)
REQUIRED_REHEARSALS = ("full", "activation", "restore", "fault_injection")

REQUIRED_REHEARSAL_CHECKPOINTS = {
    "full": ("source_capture", "import_validation", "semantic_parity", "projection_reconciliation"),
    "activation": ("writer_fence", "authority_activation", "rollback_burn", "first_admission"),
    "restore": ("backup_verified", "restore_completed", "generation_rotated", "stale_request_rejected"),
    "fault_injection": ("precommit_crash", "postcommit_replay", "projection_retry", "restart_recovery"),
}

EVIDENCE_ARTIFACT_KINDS = {
    ("authority_coverage", "current_to_target"): "authority-coverage-report",
    ("command_semantic_delta", "retained_commands"): "command-semantic-delta-report",
    ("characterization", "frozen_current_behavior"): "characterization-manifest",
    ("production_change_ledger", "source_commit_closure"): "production-change-ledger",
    ("fault_injection", "crash_boundaries"): "fault-injection-report",
    ("contention", "same_task_and_independent_tasks"): "contention-report",
    ("backup_restore", "restore_rehearsal"): "backup-restore-report",
    ("create_correlation", "lost_response_safety"): "create-correlation-report",
    ("protocol_coherence", "service_openapi_routing"): "protocol-coherence-report",
}

REHEARSAL_CHECKPOINT_EVIDENCE_KINDS = {
    kind: {checkpoint: f"{kind}-{checkpoint}-evidence" for checkpoint in checkpoints}
    for kind, checkpoints in REQUIRED_REHEARSAL_CHECKPOINTS.items()
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseAuthorityError(ValueError):
    """A release or cutover transition failed closed."""


def _utc_comparable(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_sha256(value: object, field: str) -> str:
    if not _is_sha256(value):
        raise ReleaseAuthorityError(f"{field} must be exact lowercase hexadecimal SHA-256")
    return str(value)


def _require_nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseAuthorityError(f"{field} must be a nonblank string")
    return value


def _require_at_or_after(
    value: datetime,
    floor: datetime,
    *,
    field: str,
    floor_field: str,
) -> None:
    if _utc_comparable(value) < _utc_comparable(floor):
        raise ReleaseAuthorityError(f"{field} must be at or after {floor_field}")


def _validate_evidence_payload(
    *, category: str, evidence_key: str, outcome: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    contract = (category, evidence_key)
    expected_kind = EVIDENCE_ARTIFACT_KINDS.get(contract)
    if expected_kind is None:
        raise ReleaseAuthorityError("unknown release evidence contract")
    if outcome not in {"pass", "fail"}:
        raise ReleaseAuthorityError("release evidence outcome must be pass or fail")
    body = dict(payload)
    if body.get("artifact_kind") != expected_kind:
        raise ReleaseAuthorityError("release evidence artifact kind does not match contract")
    _require_nonblank(body.get("artifact_identity"), "artifact_identity")
    _require_nonblank(body.get("artifact_path"), "artifact_path")
    _require_sha256(body.get("artifact_sha256"), "artifact_sha256")
    _require_sha256(body.get("source_manifest_sha256"), "source_manifest_sha256")
    if body.get("gate_name") != f"{category}:{evidence_key}":
        raise ReleaseAuthorityError("release evidence gate name does not match contract")
    if body.get("gate_result") != outcome:
        raise ReleaseAuthorityError("release evidence gate result does not match outcome")
    return body


def _validate_checkpoint_payload(
    *, rehearsal: rel.RehearsalRun, checkpoint_kind: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    expected = REHEARSAL_CHECKPOINT_EVIDENCE_KINDS.get(rehearsal.rehearsal_kind, {}).get(
        checkpoint_kind
    )
    if expected is None:
        raise ReleaseAuthorityError("checkpoint is not valid for rehearsal class")
    body = dict(payload)
    if body.get("rehearsal_kind") != rehearsal.rehearsal_kind:
        raise ReleaseAuthorityError("checkpoint rehearsal kind does not match run")
    if body.get("checkpoint_kind") != checkpoint_kind:
        raise ReleaseAuthorityError("checkpoint kind does not match payload")
    if body.get("evidence_kind") != expected:
        raise ReleaseAuthorityError("checkpoint evidence kind does not match contract")
    _require_nonblank(body.get("artifact_identity"), "artifact_identity")
    _require_sha256(body.get("artifact_sha256"), "artifact_sha256")
    source_manifest = _require_sha256(
        body.get("source_manifest_sha256"), "source_manifest_sha256"
    )
    if source_manifest != rehearsal.source_manifest_sha256:
        raise ReleaseAuthorityError("checkpoint source manifest does not match rehearsal")
    if body.get("gate_result") not in {"pass", "fail"}:
        raise ReleaseAuthorityError("checkpoint gate result must be pass or fail")
    return body


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class AcceptanceCheck:
    code: str
    passed: bool
    details: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "passed": self.passed, "details": dict(self.details)}


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: uuid.UUID
    checks: tuple[AcceptanceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": str(self.candidate_id),
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


class ReleaseCandidateService:
    def __init__(
        self,
        session: Session,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.uuid_factory = uuid_factory
        self.clock = clock

    def _trusted_now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        value = self.session.scalar(select(func.current_timestamp()))
        if not isinstance(value, datetime):
            raise ReleaseAuthorityError("database clock did not return a timestamp")
        return value

    def _require_not_future(self, value: datetime, field: str) -> None:
        if _utc_comparable(value) > _utc_comparable(self._trusted_now()):
            raise ReleaseAuthorityError(f"{field} cannot be later than the trusted database clock")

    def _cutover_checkpoint_time(
        self, cutover_run_id: uuid.UUID, checkpoint_kind: str
    ) -> datetime:
        checkpoint = self.session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == checkpoint_kind,
            )
        )
        if checkpoint is None:
            raise ReleaseAuthorityError(f"cutover lacks {checkpoint_kind} chronology evidence")
        return checkpoint.recorded_at

    def create_candidate(
        self,
        *,
        candidate_id: uuid.UUID,
        generation_id: uuid.UUID,
        source_import_batch_id: uuid.UUID,
        shadow_baseline_id: uuid.UUID,
        projection_epoch_id: uuid.UUID,
        source_release: str,
        source_commit: str,
        ledger_through_commit: str,
        schema_head: str,
        dish_release: str,
        honest_release: str,
        protocol_release: str,
        openapi_release: str,
        routing_release: str,
        created_at: datetime,
    ) -> rel.ReleaseCandidate:
        existing = self.session.get(rel.ReleaseCandidate, candidate_id)
        identity = {
            "generation_id": generation_id,
            "source_import_batch_id": source_import_batch_id,
            "shadow_baseline_id": shadow_baseline_id,
            "projection_epoch_id": projection_epoch_id,
            "source_release": source_release,
            "source_commit": source_commit,
            "ledger_through_commit": ledger_through_commit,
            "schema_head": schema_head,
            "dish_release": dish_release,
            "honest_release": honest_release,
            "protocol_release": protocol_release,
            "openapi_release": openapi_release,
            "routing_release": routing_release,
        }
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in identity.items()):
                raise ReleaseAuthorityError("release candidate identity conflict")
            return existing

        generation = self.session.get(models.AuthorityGeneration, generation_id)
        batch = self.session.get(tx.SourceImportBatch, source_import_batch_id)
        baseline = self.session.get(tx.ShadowBaseline, shadow_baseline_id)
        epoch = self.session.get(tx.ProjectionEpoch, projection_epoch_id)
        if generation is None or generation.status != "active":
            raise ReleaseAuthorityError("release candidate requires the active target generation")
        if batch is None or batch.generation_id != generation_id:
            raise ReleaseAuthorityError("source import batch does not belong to candidate generation")
        if baseline is None or baseline.generation_id != generation_id:
            raise ReleaseAuthorityError("shadow baseline does not belong to candidate generation")
        if epoch is None or epoch.generation_id != generation_id:
            raise ReleaseAuthorityError("projection epoch does not belong to candidate generation")
        if source_release != batch.source_release or source_commit != batch.source_commit:
            raise ReleaseAuthorityError("candidate source identity does not match import batch")
        if ledger_through_commit != batch.ledger_through_commit:
            raise ReleaseAuthorityError("candidate ledger closure does not match import batch")
        if source_commit != baseline.source_commit:
            raise ReleaseAuthorityError("candidate source commit does not match shadow baseline")
        if schema_head != ALEMBIC_HEAD:
            raise ReleaseAuthorityError(f"candidate schema head must be {ALEMBIC_HEAD}")

        row = rel.ReleaseCandidate(
            candidate_id=candidate_id,
            generation_id=generation_id,
            source_import_batch_id=source_import_batch_id,
            shadow_baseline_id=shadow_baseline_id,
            projection_epoch_id=projection_epoch_id,
            source_release=source_release,
            source_commit=source_commit,
            ledger_through_commit=ledger_through_commit,
            schema_head=schema_head,
            dish_release=dish_release,
            honest_release=honest_release,
            protocol_release=protocol_release,
            openapi_release=openapi_release,
            routing_release=routing_release,
            status="assembling",
            candidate_revision=1,
            validation_bundle_sha256=None,
            created_at=created_at,
            validated_at=None,
            approved_at=None,
            terminal_at=None,
        )
        self.session.add(row)
        # Flush the candidate before inserting the FK-dependent admission row;
        # no ORM relationship exists to order these otherwise on SQLite.
        self.session.flush()
        self.session.add(
            rel.MutationAdmissionControl(
                generation_id=generation_id,
                candidate_id=candidate_id,
                state="closed",
                control_revision=1,
                opened_at=None,
                updated_at=created_at,
            )
        )
        self.session.flush()
        return row

    def record_evidence(
        self,
        *,
        candidate_id: uuid.UUID,
        category: str,
        evidence_key: str,
        outcome: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.ReleaseEvidenceItem:
        candidate = self._candidate(candidate_id)
        if candidate.status != "assembling":
            raise ReleaseAuthorityError("release evidence is frozen after candidate validation")
        body = _validate_evidence_payload(
            category=category,
            evidence_key=evidence_key,
            outcome=outcome,
            payload=payload,
        )
        latest = self.session.scalar(
            select(rel.ReleaseEvidenceItem)
            .where(
                rel.ReleaseEvidenceItem.candidate_id == candidate_id,
                rel.ReleaseEvidenceItem.category == category,
                rel.ReleaseEvidenceItem.evidence_key == evidence_key,
            )
            .order_by(rel.ReleaseEvidenceItem.evidence_revision.desc())
            .limit(1)
        )
        digest = sha256_json(body)
        if latest is not None and latest.payload_sha256 == digest and latest.outcome == outcome:
            return latest
        revision = 1 if latest is None else latest.evidence_revision + 1
        row = rel.ReleaseEvidenceItem(
            evidence_id=self.uuid_factory(),
            candidate_id=candidate_id,
            category=category,
            evidence_key=evidence_key,
            evidence_revision=revision,
            outcome=outcome,
            payload=body,
            payload_sha256=digest,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def start_rehearsal(
        self,
        *,
        candidate_id: uuid.UUID,
        rehearsal_kind: str,
        environment_identity: str,
        source_manifest_sha256: str,
        started_at: datetime,
    ) -> rel.RehearsalRun:
        candidate = self._candidate(candidate_id)
        if candidate.status != "assembling":
            raise ReleaseAuthorityError("rehearsal evidence is frozen after candidate validation")
        if rehearsal_kind not in REQUIRED_REHEARSALS:
            raise ReleaseAuthorityError("unsupported rehearsal kind")
        _require_nonblank(environment_identity, "environment_identity")
        source_manifest_sha256 = _require_sha256(
            source_manifest_sha256, "source_manifest_sha256"
        )
        existing = self.session.scalar(
            select(rel.RehearsalRun).where(
                rel.RehearsalRun.candidate_id == candidate_id,
                rel.RehearsalRun.rehearsal_kind == rehearsal_kind,
                rel.RehearsalRun.environment_identity == environment_identity,
                rel.RehearsalRun.source_manifest_sha256 == source_manifest_sha256,
            )
        )
        if existing is not None:
            return existing
        row = rel.RehearsalRun(
            rehearsal_id=self.uuid_factory(),
            candidate_id=candidate_id,
            rehearsal_kind=rehearsal_kind,
            environment_identity=environment_identity,
            source_manifest_sha256=source_manifest_sha256,
            status="running",
            run_revision=1,
            report=None,
            report_sha256=None,
            measured_rpo_seconds=None,
            measured_rto_seconds=None,
            started_at=started_at,
            completed_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_rehearsal_checkpoint(
        self,
        *,
        rehearsal_id: uuid.UUID,
        checkpoint_kind: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.RehearsalCheckpoint:
        rehearsal = self.session.get(rel.RehearsalRun, rehearsal_id)
        if rehearsal is None or rehearsal.status != "running":
            raise ReleaseAuthorityError("rehearsal is not running")
        existing = self.session.scalar(
            select(rel.RehearsalCheckpoint).where(
                rel.RehearsalCheckpoint.rehearsal_id == rehearsal_id,
                rel.RehearsalCheckpoint.checkpoint_kind == checkpoint_kind,
            )
        )
        body = _validate_checkpoint_payload(
            rehearsal=rehearsal,
            checkpoint_kind=checkpoint_kind,
            payload=payload,
        )
        digest = sha256_json(body)
        if existing is not None:
            if existing.payload_sha256 != digest:
                raise ReleaseAuthorityError("rehearsal checkpoint identity conflict")
            return existing
        sequence = int(
            self.session.scalar(
                select(func.coalesce(func.max(rel.RehearsalCheckpoint.sequence), 0)).where(
                    rel.RehearsalCheckpoint.rehearsal_id == rehearsal_id
                )
            )
            or 0
        ) + 1
        row = rel.RehearsalCheckpoint(
            checkpoint_id=self.uuid_factory(),
            rehearsal_id=rehearsal_id,
            sequence=sequence,
            checkpoint_kind=checkpoint_kind,
            payload=body,
            payload_sha256=digest,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish_rehearsal(
        self,
        *,
        rehearsal_id: uuid.UUID,
        passed: bool,
        report: Mapping[str, Any],
        completed_at: datetime,
        measured_rpo_seconds: float | None = None,
        measured_rto_seconds: float | None = None,
    ) -> rel.RehearsalRun:
        row = self.session.get(rel.RehearsalRun, rehearsal_id)
        if row is None:
            raise ReleaseAuthorityError("unknown rehearsal")
        checkpoints = self.session.scalars(
            select(rel.RehearsalCheckpoint)
            .where(rel.RehearsalCheckpoint.rehearsal_id == rehearsal_id)
            .order_by(rel.RehearsalCheckpoint.sequence)
        ).all()
        observed = {checkpoint.checkpoint_kind for checkpoint in checkpoints}
        required = set(REQUIRED_REHEARSAL_CHECKPOINTS[row.rehearsal_kind])
        missing = sorted(required - observed)
        if passed and missing:
            raise ReleaseAuthorityError(
                "passed rehearsal lacks required checkpoints: " + ", ".join(missing)
            )
        if passed and any(
            checkpoint.payload.get("gate_result") != "pass" for checkpoint in checkpoints
        ):
            raise ReleaseAuthorityError("passed rehearsal contains a failed checkpoint")
        checkpoint_manifest_sha256 = sha256_json(
            [
                {
                    "checkpoint_kind": checkpoint.checkpoint_kind,
                    "payload_sha256": checkpoint.payload_sha256,
                }
                for checkpoint in checkpoints
            ]
        )
        body = dict(report)
        terminal = "passed" if passed else "failed"
        if body.get("rehearsal_kind") != row.rehearsal_kind:
            raise ReleaseAuthorityError("rehearsal report kind does not match run")
        if body.get("source_manifest_sha256") != row.source_manifest_sha256:
            raise ReleaseAuthorityError("rehearsal report source manifest does not match run")
        if body.get("result") != terminal:
            raise ReleaseAuthorityError("rehearsal report result does not match terminal state")
        _require_sha256(
            body.get("checkpoint_manifest_sha256"), "checkpoint_manifest_sha256"
        )
        if body["checkpoint_manifest_sha256"] != checkpoint_manifest_sha256:
            raise ReleaseAuthorityError("rehearsal report does not bind exact checkpoint set")
        digest = sha256_json(body)
        if row.status != "running":
            if row.status != terminal or row.report_sha256 != digest:
                raise ReleaseAuthorityError("rehearsal terminal result conflict")
            return row
        row.status = terminal
        row.run_revision += 1
        row.report = body
        row.report_sha256 = digest
        row.measured_rpo_seconds = measured_rpo_seconds
        row.measured_rto_seconds = measured_rto_seconds
        row.completed_at = completed_at
        self.session.flush()
        return row

    def prepare_writer_fence(
        self,
        *,
        candidate_id: uuid.UUID,
        target_identity: str,
        mechanism: str,
        manifest: Mapping[str, Any],
        prepared_at: datetime,
    ) -> rel.LegacyWriterFence:
        candidate = self._candidate(candidate_id)
        if candidate.status not in {"assembling", "validated", "approved"}:
            raise ReleaseAuthorityError("writer fence cannot be prepared for terminal candidate")
        _require_nonblank(target_identity, "target_identity")
        _require_nonblank(mechanism, "mechanism")
        _require_at_or_after(
            prepared_at,
            candidate.created_at,
            field="prepared_at",
            floor_field="candidate created_at",
        )
        self._require_not_future(prepared_at, "prepared_at")
        digest = sha256_json(dict(manifest))
        existing = self.session.scalar(
            select(rel.LegacyWriterFence).where(
                rel.LegacyWriterFence.candidate_id == candidate_id,
                rel.LegacyWriterFence.target_identity == target_identity,
            )
        )
        if existing is not None:
            if existing.manifest_sha256 != digest or existing.mechanism != mechanism:
                raise ReleaseAuthorityError("writer fence identity conflict")
            return existing
        row = rel.LegacyWriterFence(
            fence_id=self.uuid_factory(),
            candidate_id=candidate_id,
            target_identity=target_identity,
            mechanism=mechanism,
            manifest_sha256=digest,
            state="prepared",
            fence_revision=1,
            proof_sha256=None,
            prepared_at=prepared_at,
            engaged_at=None,
            verified_at=None,
            released_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def engage_writer_fence(self, *, fence_id: uuid.UUID, engaged_at: datetime) -> rel.LegacyWriterFence:
        row = self._fence(fence_id)
        if row.state == "engaged" or row.state == "verified":
            return row
        if row.state != "prepared":
            raise ReleaseAuthorityError("writer fence is not prepared")
        _require_at_or_after(
            engaged_at,
            row.prepared_at,
            field="engaged_at",
            floor_field="prepared_at",
        )
        self._require_not_future(engaged_at, "engaged_at")
        row.state = "engaged"
        row.fence_revision += 1
        row.engaged_at = engaged_at
        self.session.flush()
        return row

    def verify_writer_fence(
        self,
        *,
        fence_id: uuid.UUID,
        proof: Mapping[str, Any],
        verified_at: datetime,
    ) -> rel.LegacyWriterFence:
        row = self._fence(fence_id)
        candidate = self._candidate(row.candidate_id)
        body = dict(proof)
        required = {
            "probe_kind": "authenticated_mutation_rejected_before_body_parse",
            "candidate_id": str(candidate.candidate_id),
            "target_identity": row.target_identity,
            "fence_manifest_sha256": row.manifest_sha256,
            "http_status": 409,
            "response_code": "CONFLICT",
            "response_rule": "legacy_writer_fenced",
            "response_retryable": False,
            "result": "pass",
            "body_loaded": False,
        }
        if any(body.get(key) != value for key, value in required.items()):
            raise ReleaseAuthorityError(
                "writer fence proof does not match the exact authenticated mutation response"
            )
        _require_sha256(body.get("request_token_sha256"), "request_token_sha256")
        if row.engaged_at is None:
            raise ReleaseAuthorityError("writer fence lacks engagement chronology")
        _require_at_or_after(
            verified_at,
            row.engaged_at,
            field="verified_at",
            floor_field="engaged_at",
        )
        self._require_not_future(verified_at, "verified_at")
        digest = sha256_json(body)
        if row.state == "verified":
            if row.proof_sha256 != digest:
                raise ReleaseAuthorityError("writer fence proof conflict")
            return row
        if row.state != "engaged":
            raise ReleaseAuthorityError("writer fence must be engaged before verification")
        row.state = "verified"
        row.fence_revision += 1
        row.proof_sha256 = digest
        row.verified_at = verified_at
        self.session.flush()
        return row

    def release_writer_fence(self, *, fence_id: uuid.UUID, released_at: datetime) -> rel.LegacyWriterFence:
        row = self._fence(fence_id)
        candidate = self._candidate(row.candidate_id)
        activation = self.session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == candidate.generation_id,
                models.AuthorityActivation.outcome == "activated",
            )
        )
        if activation is not None:
            raise ReleaseAuthorityError("writer fence cannot be released after rollback burn")
        if candidate.status != "aborted":
            raise ReleaseAuthorityError("writer fence release requires an aborted candidate")
        if row.state == "released":
            return row
        if row.state not in {"engaged", "verified"}:
            raise ReleaseAuthorityError("writer fence is not engaged")
        floor = row.verified_at or row.engaged_at
        if floor is None:
            raise ReleaseAuthorityError("writer fence lacks release chronology")
        _require_at_or_after(
            released_at,
            floor,
            field="released_at",
            floor_field="latest writer fence transition",
        )
        self._require_not_future(released_at, "released_at")
        row.state = "released"
        row.fence_revision += 1
        row.released_at = released_at
        self.session.flush()
        return row

    def evaluate_candidate(self, *, candidate_id: uuid.UUID) -> CandidateEvaluation:
        candidate = self._candidate(candidate_id)
        checks: list[AcceptanceCheck] = []

        def add(code: str, passed: bool, **details: Any) -> None:
            checks.append(AcceptanceCheck(code, bool(passed), details))

        generation = self.session.get(models.AuthorityGeneration, candidate.generation_id)
        add(
            "generation_active",
            generation is not None and generation.status == "active",
            status=None if generation is None else generation.status,
        )

        batch = self.session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
        batch_ok = (
            batch is not None
            and batch.generation_id == candidate.generation_id
            and batch.status == "complete"
            and batch.imported_entities == batch.expected_entities
            and batch.source_release == candidate.source_release
            and batch.source_commit == candidate.source_commit
            and batch.ledger_through_commit == candidate.ledger_through_commit
        )
        add(
            "final_import_complete",
            batch_ok,
            status=None if batch is None else batch.status,
            imported=None if batch is None else batch.imported_entities,
            expected=None if batch is None else batch.expected_entities,
        )

        baseline = self.session.get(tx.ShadowBaseline, candidate.shadow_baseline_id)
        unresolved_deliveries = self._count(
            tx.ShadowDelivery,
            tx.ShadowDelivery.state.in_(("pending", "claimed", "failed")),
            join=(tx.ShadowEnvelope, tx.ShadowDelivery.envelope_id == tx.ShadowEnvelope.envelope_id),
            extra=(tx.ShadowEnvelope.shadow_baseline_id == candidate.shadow_baseline_id,),
        )
        unresolved_gaps = self._count(
            tx.ShadowGap,
            tx.ShadowGap.shadow_baseline_id == candidate.shadow_baseline_id,
            tx.ShadowGap.resolved_at.is_(None),
        )
        add(
            "shadow_gap_free",
            baseline is not None
            and baseline.status == "closed"
            and unresolved_deliveries == 0
            and unresolved_gaps == 0,
            baseline_status=None if baseline is None else baseline.status,
            unresolved_deliveries=unresolved_deliveries,
            unresolved_gaps=unresolved_gaps,
        )

        epoch = self.session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
        add(
            "projection_epoch_ready",
            epoch is not None
            and epoch.generation_id == candidate.generation_id
            and epoch.status == "active",
            status=None if epoch is None else epoch.status,
        )

        registry = self.session.get(models.ActiveSectionRegistry, candidate.generation_id)
        add("active_registry_present", registry is not None)

        total_tasks = self._count(
            models.TaskAuthorityHead,
            models.TaskAuthorityHead.generation_id == candidate.generation_id,
        )
        placements = self._count(
            models.CurrentTaskSectionPlacement,
            models.CurrentTaskSectionPlacement.generation_id == candidate.generation_id,
        )
        completions = self._count(
            models.CurrentTaskCompletion,
            models.CurrentTaskCompletion.generation_id == candidate.generation_id,
        )
        membership_tasks = int(
            self.session.scalar(
                select(func.count(func.distinct(models.CurrentTaskProjectMembership.task_id))).where(
                    models.CurrentTaskProjectMembership.generation_id == candidate.generation_id,
                    models.CurrentTaskProjectMembership.is_member.is_(True),
                )
            )
            or 0
        )
        alias_tasks = int(
            self.session.scalar(
                select(func.count(func.distinct(models.TaskExternalAlias.task_id)))
                .join(models.TaskAuthorityHead, models.TaskAuthorityHead.task_id == models.TaskExternalAlias.task_id)
                .where(
                    models.TaskAuthorityHead.generation_id == candidate.generation_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                )
            )
            or 0
        )
        add(
            "task_corpus_closed",
            total_tasks > 0
            and placements == total_tasks
            and completions == total_tasks
            and membership_tasks == total_tasks
            and alias_tasks == total_tasks,
            tasks=total_tasks,
            placements=placements,
            completions=completions,
            membership_tasks=membership_tasks,
            alias_tasks=alias_tasks,
        )

        registry_section_ids: tuple[uuid.UUID, ...] = ()
        registry_project_ids: tuple[uuid.UUID, ...] = ()
        if registry is not None:
            registry_section_ids = tuple(
                self.session.scalars(
                    select(models.SectionRegistryEntry.section_id).where(
                        models.SectionRegistryEntry.registry_version_id == registry.registry_version_id
                    )
                )
            )
            if registry_section_ids:
                registry_project_ids = tuple(
                    self.session.scalars(
                        select(models.GovernedSection.project_id)
                        .where(models.GovernedSection.section_id.in_(registry_section_ids))
                        .distinct()
                    )
                )
        active_projects = len(registry_project_ids)
        aliased_projects = int(
            self.session.scalar(
                select(func.count(func.distinct(models.ProjectExternalAlias.project_id))).where(
                    models.ProjectExternalAlias.project_id.in_(registry_project_ids or (uuid.UUID(int=0),)),
                    models.ProjectExternalAlias.external_system == "asana",
                    models.ProjectExternalAlias.state == "active",
                )
            )
            or 0
        )
        active_sections = len(registry_section_ids)
        aliased_sections = int(
            self.session.scalar(
                select(func.count(func.distinct(models.SectionExternalAlias.section_id))).where(
                    models.SectionExternalAlias.section_id.in_(registry_section_ids or (uuid.UUID(int=0),)),
                    models.SectionExternalAlias.external_system == "asana",
                    models.SectionExternalAlias.state == "active",
                )
            )
            or 0
        )
        add(
            "registry_alias_corpus_closed",
            active_projects == aliased_projects and active_sections == aliased_sections,
            active_projects=active_projects,
            aliased_projects=aliased_projects,
            active_sections=active_sections,
            aliased_sections=aliased_sections,
        )

        open_counts = {
            "requests_without_outcome": int(
                self.session.scalar(
                    select(func.count())
                    .select_from(wf.ServiceRequest)
                    .outerjoin(wf.ServiceRequestOutcome, wf.ServiceRequestOutcome.request_id == wf.ServiceRequest.request_id)
                    .where(
                        wf.ServiceRequest.generation_id == candidate.generation_id,
                        wf.ServiceRequestOutcome.outcome_id.is_(None),
                    )
                )
                or 0
            ),
            "executions": self._count(
                wf.CommandExecution,
                wf.CommandExecution.generation_id == candidate.generation_id,
                wf.CommandExecution.status.in_(("pending", "claimed", "uncertain")),
            ),
            "operations": self._count(
                wf.WorkflowOperation,
                wf.WorkflowOperation.generation_id == candidate.generation_id,
                wf.WorkflowOperation.lifecycle == "open",
            ),
            "leases": self._count(
                wf.ServiceLease,
                wf.ServiceLease.generation_id == candidate.generation_id,
                wf.ServiceLease.state == "active",
            ),
            "planning_challenges": self._count(
                wf.PlanningIntentChallenge,
                wf.PlanningIntentChallenge.generation_id == candidate.generation_id,
                wf.PlanningIntentChallenge.state.in_(("issued", "claimed")),
            ),
            "authorization_reservations": self._count(
                wf.MarcoAuthorizationState,
                wf.MarcoAuthorizationState.state == "reserved",
                join=(wf.MarcoAuthorizationGrant, wf.MarcoAuthorizationState.grant_id == wf.MarcoAuthorizationGrant.grant_id),
                extra=(wf.MarcoAuthorizationGrant.generation_id == candidate.generation_id,),
            ),
            "verification_cycles": self._count(
                wf.VerificationCycle,
                wf.VerificationCycle.generation_id == candidate.generation_id,
                wf.VerificationCycle.lifecycle == "open",
            ),
            "evidence_holds": self._count(
                wf.EvidenceHold,
                wf.EvidenceHold.generation_id == candidate.generation_id,
                wf.EvidenceHold.state == "open",
            ),
            "human_reviews": self._count(
                wf.HumanReviewRequirement,
                wf.HumanReviewRequirement.generation_id == candidate.generation_id,
                wf.HumanReviewRequirement.state == "open",
            ),
            "abandonments": self._count(
                wf.AbandonmentAttempt,
                wf.AbandonmentAttempt.generation_id == candidate.generation_id,
                wf.AbandonmentAttempt.state.in_(("preparing", "published", "blocked", "reconciling")),
            ),
            "audit_obligations": self._count(
                wf.InvocationAuditObligation,
                wf.InvocationAuditObligation.generation_id == candidate.generation_id,
                wf.InvocationAuditObligation.state == "pending",
            ),
        }
        add("legacy_and_target_authority_resolved", not any(open_counts.values()), **open_counts)

        projection_counts = {
            "outbox": self._count(
                tx.ProjectionOutboxEvent,
                tx.ProjectionOutboxEvent.generation_id == candidate.generation_id,
                tx.ProjectionOutboxEvent.state.in_(("pending", "claimed", "uncertain", "blocked")),
            ),
            "attempts": self._count(
                tx.ProjectionAttempt,
                tx.ProjectionAttempt.state.in_(("dispatched", "uncertain", "blocked")),
                join=(tx.ProjectionOutboxEvent, tx.ProjectionAttempt.projection_event_id == tx.ProjectionOutboxEvent.projection_event_id),
                extra=(tx.ProjectionOutboxEvent.generation_id == candidate.generation_id,),
            ),
            "create_correlations": self._count(
                tx.ProjectionCreateCorrelation,
                tx.ProjectionCreateCorrelation.state.in_(("pending", "ambiguous")),
                join=(tx.ProjectionOutboxEvent, tx.ProjectionCreateCorrelation.projection_event_id == tx.ProjectionOutboxEvent.projection_event_id),
                extra=(tx.ProjectionOutboxEvent.generation_id == candidate.generation_id,),
            ),
            "drift": self._count(
                tx.ProjectionDriftEvent,
                tx.ProjectionDriftEvent.generation_id == candidate.generation_id,
                tx.ProjectionDriftEvent.state == "open",
            ),
        }
        latest_reconciliation = self.session.scalar(
            select(tx.ProjectionReconciliationRun)
            .where(
                tx.ProjectionReconciliationRun.generation_id == candidate.generation_id,
                tx.ProjectionReconciliationRun.projection_epoch_id == candidate.projection_epoch_id,
            )
            .order_by(tx.ProjectionReconciliationRun.started_at.desc())
            .limit(1)
        )
        active_mapping_count = sum(
            self._count(
                mapping_model,
                mapping_model.generation_id == candidate.generation_id,
                mapping_model.projection_epoch_id == candidate.projection_epoch_id,
                mapping_model.state == "active",
            )
            for mapping_model in (
                tx.ProjectProjectionMapping,
                tx.SectionProjectionMapping,
                tx.TaskProjectionMapping,
            )
        )
        reconciled_mapping_count = 0
        if latest_reconciliation is not None:
            reconciled_mapping_count = int(
                self.session.scalar(
                    select(func.count(func.distinct(tx.ProjectionReconciliationItem.mapping_id))).where(
                        tx.ProjectionReconciliationItem.reconciliation_run_id
                        == latest_reconciliation.reconciliation_run_id,
                        tx.ProjectionReconciliationItem.mapping_id.is_not(None),
                        tx.ProjectionReconciliationItem.outcome.in_(("matched", "reprojected")),
                    )
                )
                or 0
            )
        reconciliation_ok = (
            latest_reconciliation is not None
            and latest_reconciliation.status == "complete"
            and latest_reconciliation.processed_items == latest_reconciliation.expected_items
            and latest_reconciliation.expected_items >= active_mapping_count
            and reconciled_mapping_count == active_mapping_count
        )
        add(
            "projection_ready",
            not any(projection_counts.values()) and reconciliation_ok,
            **projection_counts,
            active_mappings=active_mapping_count,
            reconciled_mappings=reconciled_mapping_count,
            reconciliation_expected=None
            if latest_reconciliation is None
            else latest_reconciliation.expected_items,
            reconciliation_processed=None
            if latest_reconciliation is None
            else latest_reconciliation.processed_items,
            reconciliation_status=None if latest_reconciliation is None else latest_reconciliation.status,
        )

        schema_version: str | None = None
        try:
            if inspect(self.session.get_bind()).has_table("alembic_version"):
                schema_version = self.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        except Exception:
            schema_version = None
        add(
            "schema_at_release_head",
            candidate.schema_head == ALEMBIC_HEAD and schema_version == ALEMBIC_HEAD,
            candidate_schema_head=candidate.schema_head,
            database_schema_head=schema_version,
        )

        latest_evidence: dict[tuple[str, str], rel.ReleaseEvidenceItem] = {}
        for item in self.session.scalars(
            select(rel.ReleaseEvidenceItem)
            .where(rel.ReleaseEvidenceItem.candidate_id == candidate_id)
            .order_by(
                rel.ReleaseEvidenceItem.category,
                rel.ReleaseEvidenceItem.evidence_key,
                rel.ReleaseEvidenceItem.evidence_revision,
            )
        ):
            latest_evidence[(item.category, item.evidence_key)] = item
        evidence_status = {
            f"{category}:{key}": (
                None if latest_evidence.get((category, key)) is None else latest_evidence[(category, key)].outcome
            )
            for category, key in REQUIRED_EVIDENCE
        }
        add(
            "required_acceptance_evidence",
            all(value == "pass" for value in evidence_status.values()),
            evidence=evidence_status,
        )

        rehearsal_status: dict[str, str | None] = {}
        for kind in REQUIRED_REHEARSALS:
            latest = self.session.scalar(
                select(rel.RehearsalRun)
                .where(
                    rel.RehearsalRun.candidate_id == candidate_id,
                    rel.RehearsalRun.rehearsal_kind == kind,
                )
                .order_by(rel.RehearsalRun.started_at.desc())
                .limit(1)
            )
            rehearsal_status[kind] = None if latest is None else latest.status
        add(
            "required_rehearsals",
            all(value == "passed" for value in rehearsal_status.values()),
            rehearsals=rehearsal_status,
        )

        return CandidateEvaluation(candidate_id, tuple(checks))

    def build_evidence_bundle(
        self,
        *,
        candidate_id: uuid.UUID,
        bundle_kind: str,
        built_at: datetime,
    ) -> rel.EvidenceBundle:
        candidate = self._candidate(candidate_id)
        evaluation = self.evaluate_candidate(candidate_id=candidate_id)
        evidence = self.session.scalars(
            select(rel.ReleaseEvidenceItem)
            .where(rel.ReleaseEvidenceItem.candidate_id == candidate_id)
            .order_by(
                rel.ReleaseEvidenceItem.category,
                rel.ReleaseEvidenceItem.evidence_key,
                rel.ReleaseEvidenceItem.evidence_revision,
            )
        ).all()
        rehearsals = self.session.scalars(
            select(rel.RehearsalRun)
            .where(rel.RehearsalRun.candidate_id == candidate_id)
            .order_by(rel.RehearsalRun.rehearsal_kind, rel.RehearsalRun.started_at)
        ).all()
        fences = self.session.scalars(
            select(rel.LegacyWriterFence)
            .where(rel.LegacyWriterFence.candidate_id == candidate_id)
            .order_by(rel.LegacyWriterFence.target_identity)
        ).all()
        checkpoints = self.session.scalars(
            select(rel.CutoverCheckpoint)
            .join(rel.CutoverRun, rel.CutoverRun.cutover_run_id == rel.CutoverCheckpoint.cutover_run_id)
            .where(rel.CutoverRun.candidate_id == candidate_id)
            .order_by(rel.CutoverCheckpoint.sequence)
        ).all()
        approval = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate_id)
        )
        activation = self.session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == candidate.generation_id,
                models.AuthorityActivation.outcome == "activated",
            )
        )
        manifest = {
            "format": "dish-stage6-evidence-bundle-v1",
            "bundle_kind": bundle_kind,
            "candidate": {
                "candidate_id": str(candidate.candidate_id),
                "generation_id": str(candidate.generation_id),
                "source_import_batch_id": str(candidate.source_import_batch_id),
                "shadow_baseline_id": str(candidate.shadow_baseline_id),
                "projection_epoch_id": str(candidate.projection_epoch_id),
                "source_release": candidate.source_release,
                "source_commit": candidate.source_commit,
                "ledger_through_commit": candidate.ledger_through_commit,
                "schema_head": candidate.schema_head,
                "dish_release": candidate.dish_release,
                "honest_release": candidate.honest_release,
                "protocol_release": candidate.protocol_release,
                "openapi_release": candidate.openapi_release,
                "routing_release": candidate.routing_release,
                "status": candidate.status,
            },
            "acceptance": evaluation.as_dict(),
            "evidence": [
                {
                    "category": item.category,
                    "key": item.evidence_key,
                    "revision": item.evidence_revision,
                    "outcome": item.outcome,
                    "sha256": item.payload_sha256,
                }
                for item in evidence
            ],
            "rehearsals": [
                {
                    "rehearsal_id": str(run.rehearsal_id),
                    "kind": run.rehearsal_kind,
                    "environment": run.environment_identity,
                    "source_manifest_sha256": run.source_manifest_sha256,
                    "status": run.status,
                    "report_sha256": run.report_sha256,
                    "measured_rpo_seconds": run.measured_rpo_seconds,
                    "measured_rto_seconds": run.measured_rto_seconds,
                }
                for run in rehearsals
            ],
            "writer_fences": [
                {
                    "fence_id": str(fence.fence_id),
                    "target": fence.target_identity,
                    "mechanism": fence.mechanism,
                    "manifest_sha256": fence.manifest_sha256,
                    "state": fence.state,
                    "proof_sha256": fence.proof_sha256,
                }
                for fence in fences
            ],
            "cutover_checkpoints": [
                {
                    "sequence": checkpoint.sequence,
                    "kind": checkpoint.checkpoint_kind,
                    "sha256": checkpoint.payload_sha256,
                }
                for checkpoint in checkpoints
            ],
            "approval": None
            if approval is None
            else {
                "approval_id": str(approval.approval_id),
                "approver": approval.approver,
                "approval_sha256": approval.approval_sha256,
                "approved_at": approval.approved_at.isoformat(),
            },
            "activation": None
            if activation is None
            else {
                "activation_id": str(activation.activation_id),
                "rollback_burned_at": activation.rollback_burned_at.isoformat()
                if activation.rollback_burned_at
                else None,
            },
        }
        if bundle_kind == "cutover_final":
            closures = self.session.scalars(
                select(rel.FinalAsanaClosure)
                .where(rel.FinalAsanaClosure.candidate_id == candidate_id)
                .order_by(rel.FinalAsanaClosure.closed_through_at, rel.FinalAsanaClosure.recorded_at)
            ).all()
            manifest["final_asana_closures"] = [
                {
                    "closure_id": str(closure.closure_id),
                    "capture_manifest_sha256": closure.capture_manifest_sha256,
                    "observation_high_water": closure.observation_high_water,
                    "closed_through_at": closure.closed_through_at.isoformat(),
                    "closure_sha256": closure.closure_sha256,
                    "invalidated": self._closure_invalidation(closure.closure_id) is not None,
                }
                for closure in closures
            ]
            manifest["recertifications"] = [
                {
                    "recertification_id": str(row.recertification_id),
                    "closure_id": str(row.closure_id),
                    "revision": row.recertification_revision,
                    "sha256": row.recertification_sha256,
                }
                for row in self.session.scalars(
                    select(rel.CutoverRecertification)
                    .where(rel.CutoverRecertification.candidate_id == candidate_id)
                    .order_by(rel.CutoverRecertification.recertification_revision)
                ).all()
            ]
        if bundle_kind == "cutover_final":
            runtime = self.session.scalar(
                select(rel.RuntimeReleaseAttestation).where(
                    rel.RuntimeReleaseAttestation.candidate_id == candidate_id
                )
            )
            worker = self.session.scalar(
                select(rel.ProjectionWorkerReadiness).where(
                    rel.ProjectionWorkerReadiness.candidate_id == candidate_id
                )
            )
            plan = self.session.scalar(
                select(rel.FirstAdmissionPlan)
                .join(rel.CutoverRun, rel.CutoverRun.cutover_run_id == rel.FirstAdmissionPlan.cutover_run_id)
                .where(rel.CutoverRun.candidate_id == candidate_id)
            )
            manifest["runtime_attestation"] = None if runtime is None else {
                "attestation_id": str(runtime.attestation_id),
                "sha256": runtime.attestation_sha256,
            }
            manifest["projection_worker_readiness"] = None if worker is None else {
                "readiness_id": str(worker.readiness_id),
                "sha256": worker.readiness_sha256,
                "reconciliation_run_id": str(worker.reconciliation_run_id),
            }
            manifest["first_admission_plan"] = None if plan is None else {
                "plan_id": str(plan.plan_id),
                "request_id": str(plan.request_id),
                "sha256": plan.plan_sha256,
            }
        digest = sha256_json(manifest)
        existing = self.session.scalar(
            select(rel.EvidenceBundle).where(
                rel.EvidenceBundle.candidate_id == candidate_id,
                rel.EvidenceBundle.manifest_sha256 == digest,
            )
        )
        if existing is not None:
            return existing
        revision = int(
            self.session.scalar(
                select(func.coalesce(func.max(rel.EvidenceBundle.bundle_revision), 0)).where(
                    rel.EvidenceBundle.candidate_id == candidate_id,
                    rel.EvidenceBundle.bundle_kind == bundle_kind,
                )
            )
            or 0
        ) + 1
        row = rel.EvidenceBundle(
            bundle_id=self.uuid_factory(),
            candidate_id=candidate_id,
            bundle_kind=bundle_kind,
            bundle_revision=revision,
            manifest=manifest,
            manifest_sha256=digest,
            built_at=built_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def validate_candidate(
        self,
        *,
        candidate_id: uuid.UUID,
        evidence_bundle_id: uuid.UUID,
        validated_at: datetime,
    ) -> CandidateEvaluation:
        candidate = self._candidate(candidate_id)
        bundle = self.session.get(rel.EvidenceBundle, evidence_bundle_id)
        if bundle is None or bundle.candidate_id != candidate_id or bundle.bundle_kind != "release_candidate":
            raise ReleaseAuthorityError("validation bundle does not belong to release candidate")
        evaluation = self.evaluate_candidate(candidate_id=candidate_id)
        if not evaluation.passed:
            failed = [check.code for check in evaluation.checks if not check.passed]
            raise ReleaseAuthorityError("release candidate acceptance failed: " + ", ".join(failed))
        current_bundle = self.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=validated_at,
        )
        if current_bundle.manifest_sha256 != bundle.manifest_sha256:
            raise ReleaseAuthorityError("validation bundle is stale for current release evidence")
        if candidate.status == "validated":
            if candidate.validation_bundle_sha256 != bundle.manifest_sha256:
                raise ReleaseAuthorityError("release candidate validation bundle conflict")
            return evaluation
        if candidate.status != "assembling":
            raise ReleaseAuthorityError("release candidate cannot be validated in current state")
        candidate.status = "validated"
        candidate.candidate_revision += 1
        candidate.validation_bundle_sha256 = bundle.manifest_sha256
        candidate.validated_at = validated_at
        self.session.flush()
        return evaluation

    def record_final_asana_closure(
        self,
        *,
        candidate_id: uuid.UUID,
        capture_manifest_sha256: str,
        observation_high_water: str,
        watcher_identity: str,
        interval_started_at: datetime,
        closed_through_at: datetime,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.FinalAsanaClosure:
        candidate = self._candidate(candidate_id)
        if candidate.status not in {"validated", "approved"}:
            raise ReleaseAuthorityError("final Asana closure requires a validated candidate")
        capture_manifest_sha256 = _require_sha256(
            capture_manifest_sha256, "capture_manifest_sha256"
        )
        _require_nonblank(observation_high_water, "observation_high_water")
        _require_nonblank(watcher_identity, "watcher_identity")
        _require_at_or_after(
            closed_through_at,
            interval_started_at,
            field="closed_through_at",
            floor_field="interval_started_at",
        )
        _require_at_or_after(
            recorded_at,
            closed_through_at,
            field="recorded_at",
            floor_field="closed_through_at",
        )
        self._require_not_future(recorded_at, "recorded_at")
        body = {
            "candidate_id": str(candidate_id),
            "capture_manifest_sha256": capture_manifest_sha256,
            "observation_high_water": observation_high_water,
            "watcher_identity": watcher_identity,
            "interval_started_at": interval_started_at.isoformat(),
            "closed_through_at": closed_through_at.isoformat(),
            "payload": dict(payload),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.FinalAsanaClosure).where(
                rel.FinalAsanaClosure.candidate_id == candidate_id,
                rel.FinalAsanaClosure.closure_sha256 == digest,
            )
        )
        if existing is not None:
            return existing
        row = rel.FinalAsanaClosure(
            closure_id=self.uuid_factory(),
            candidate_id=candidate_id,
            capture_manifest_sha256=capture_manifest_sha256,
            observation_high_water=observation_high_water.strip(),
            watcher_identity=watcher_identity.strip(),
            interval_started_at=interval_started_at,
            closed_through_at=closed_through_at,
            payload=dict(payload),
            closure_sha256=digest,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def invalidate_final_asana_closure(
        self,
        *,
        closure_id: uuid.UUID,
        change_identity: str,
        change_kind: str,
        payload: Mapping[str, Any],
        observed_at: datetime,
        recorded_at: datetime,
    ) -> rel.FinalAsanaClosureInvalidation:
        closure = self.session.get(rel.FinalAsanaClosure, closure_id)
        if closure is None:
            raise ReleaseAuthorityError("unknown final Asana closure")
        _require_nonblank(change_identity, "change_identity")
        _require_nonblank(change_kind, "change_kind")
        _require_at_or_after(
            observed_at,
            closure.interval_started_at,
            field="observed_at",
            floor_field="closure.interval_started_at",
        )
        _require_at_or_after(
            recorded_at,
            observed_at,
            field="recorded_at",
            floor_field="observed_at",
        )
        _require_at_or_after(
            recorded_at,
            closure.recorded_at,
            field="recorded_at",
            floor_field="closure.recorded_at",
        )
        self._require_not_future(recorded_at, "recorded_at")
        body = {
            "closure_id": str(closure_id),
            "change_identity": change_identity,
            "change_kind": change_kind,
            "payload": dict(payload),
            "observed_at": observed_at.isoformat(),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.FinalAsanaClosureInvalidation).where(
                rel.FinalAsanaClosureInvalidation.closure_id == closure_id
            )
        )
        if existing is not None:
            if existing.invalidation_sha256 != digest:
                raise ReleaseAuthorityError("final Asana closure invalidation conflict")
            return existing
        row = rel.FinalAsanaClosureInvalidation(
            invalidation_id=self.uuid_factory(),
            closure_id=closure_id,
            change_identity=change_identity.strip(),
            change_kind=change_kind.strip(),
            payload=dict(payload),
            invalidation_sha256=digest,
            observed_at=observed_at,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def recertify_candidate(
        self,
        *,
        candidate_id: uuid.UUID,
        closure_id: uuid.UUID,
        approver: str,
        recertification_statement: str,
        payload: Mapping[str, Any],
        recertified_at: datetime,
    ) -> rel.CutoverRecertification:
        candidate = self._candidate(candidate_id)
        if candidate.status != "approved":
            raise ReleaseAuthorityError("recertification requires an approved candidate")
        approval = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate_id)
        )
        closure = self._valid_final_asana_closure(candidate_id, closure_id)
        if approval is None:
            raise ReleaseAuthorityError("candidate has no approval to recertify")
        _require_at_or_after(
            recertified_at,
            closure.recorded_at,
            field="recertified_at",
            floor_field="final closure recorded_at",
        )
        _require_at_or_after(
            recertified_at,
            approval.approved_at,
            field="recertified_at",
            floor_field="approved_at",
        )
        self._require_not_future(recertified_at, "recertified_at")
        revision = int(
            self.session.scalar(
                select(func.coalesce(func.max(rel.CutoverRecertification.recertification_revision), 0)).where(
                    rel.CutoverRecertification.candidate_id == candidate_id
                )
            ) or 0
        ) + 1
        body = {
            "candidate_id": str(candidate_id),
            "approval_id": str(approval.approval_id),
            "closure_id": str(closure.closure_id),
            "closure_sha256": closure.closure_sha256,
            "revision": revision,
            "approver": approver,
            "statement": recertification_statement,
            "payload": dict(payload),
            "recertified_at": recertified_at.isoformat(),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.CutoverRecertification).where(
                rel.CutoverRecertification.closure_id == closure_id
            )
        )
        if existing is not None:
            if existing.recertification_sha256 != digest:
                raise ReleaseAuthorityError("cutover recertification identity conflict")
            return existing
        row = rel.CutoverRecertification(
            recertification_id=self.uuid_factory(),
            candidate_id=candidate_id,
            approval_id=approval.approval_id,
            closure_id=closure.closure_id,
            recertification_revision=revision,
            approver=approver.strip(),
            recertification_statement=recertification_statement.strip(),
            payload=dict(payload),
            recertification_sha256=digest,
            recertified_at=recertified_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def approve_candidate(
        self,
        *,
        candidate_id: uuid.UUID,
        evidence_bundle_id: uuid.UUID,
        approver: str,
        approval_statement: str,
        approval_payload: Mapping[str, Any],
        approved_at: datetime,
    ) -> rel.CutoverApproval:
        candidate = self._candidate(candidate_id)
        bundle = self.session.get(rel.EvidenceBundle, evidence_bundle_id)
        if candidate.status not in {"validated", "approved"}:
            raise ReleaseAuthorityError("candidate must be validated before approval")
        if (
            bundle is None
            or bundle.candidate_id != candidate_id
            or bundle.manifest_sha256 != candidate.validation_bundle_sha256
        ):
            raise ReleaseAuthorityError("approval must bind the exact validated evidence bundle")
        closure_id_value = approval_payload.get("final_asana_closure_id")
        closure_sha_value = approval_payload.get("final_asana_closure_sha256")
        if closure_id_value is None or closure_sha_value is None:
            raise ReleaseAuthorityError("approval must bind the exact final Asana closure")
        closure_sha_value = _require_sha256(
            closure_sha_value, "final_asana_closure_sha256"
        )
        try:
            closure_id = uuid.UUID(str(closure_id_value))
        except ValueError as exc:
            raise ReleaseAuthorityError("approval final Asana closure identity is invalid") from exc
        closure = self._valid_final_asana_closure(candidate_id, closure_id)
        if closure.closure_sha256 != str(closure_sha_value):
            raise ReleaseAuthorityError("approval final Asana closure digest mismatch")
        if candidate.validated_at is None:
            raise ReleaseAuthorityError("validated candidate lacks validation chronology")
        _require_at_or_after(
            approved_at,
            candidate.validated_at,
            field="approved_at",
            floor_field="validated_at",
        )
        _require_at_or_after(
            approved_at,
            closure.recorded_at,
            field="approved_at",
            floor_field="final closure recorded_at",
        )
        self._require_not_future(approved_at, "approved_at")
        body = {
            "candidate_id": str(candidate_id),
            "evidence_bundle_sha256": bundle.manifest_sha256,
            "approver": approver,
            "statement": approval_statement,
            "payload": dict(approval_payload),
            "approved_at": approved_at.isoformat(),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate_id)
        )
        if existing is not None:
            if existing.approval_sha256 != digest:
                raise ReleaseAuthorityError("cutover approval identity conflict")
            return existing
        row = rel.CutoverApproval(
            approval_id=self.uuid_factory(),
            candidate_id=candidate_id,
            evidence_bundle_id=evidence_bundle_id,
            approver=approver.strip(),
            approval_statement=approval_statement.strip(),
            approval_payload=dict(approval_payload),
            approval_sha256=digest,
            approved_at=approved_at,
        )
        self.session.add(row)
        candidate.status = "approved"
        candidate.candidate_revision += 1
        candidate.approved_at = approved_at
        self.session.flush()
        return row

    def prepare_cutover(
        self,
        *,
        candidate_id: uuid.UUID,
        started_at: datetime,
    ) -> rel.CutoverRun:
        candidate = self._candidate(candidate_id)
        if candidate.status != "approved":
            raise ReleaseAuthorityError("cutover requires an approved candidate")
        approved_closure = self._current_approved_final_asana_closure(candidate_id)
        if candidate.approved_at is None:
            raise ReleaseAuthorityError("approved candidate lacks approval chronology")
        _require_at_or_after(
            started_at,
            candidate.approved_at,
            field="started_at",
            floor_field="approved_at",
        )
        _require_at_or_after(
            started_at,
            approved_closure.recorded_at,
            field="started_at",
            floor_field="final closure recorded_at",
        )
        self._require_not_future(started_at, "started_at")
        existing = self.session.scalar(
            select(rel.CutoverRun).where(rel.CutoverRun.candidate_id == candidate_id)
        )
        if existing is not None:
            return existing
        row = rel.CutoverRun(
            cutover_run_id=self.uuid_factory(),
            candidate_id=candidate_id,
            state="prepared",
            state_revision=1,
            started_at=started_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        self._checkpoint(
            row,
            "cutover_prepared",
            {
                "candidate_id": str(candidate_id),
                "final_asana_closure_id": str(approved_closure.closure_id),
                "final_asana_closure_sha256": approved_closure.closure_sha256,
            },
            started_at,
        )
        return row

    def mark_fenced(self, *, cutover_run_id: uuid.UUID, recorded_at: datetime) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        if run.state == "fenced":
            return run
        if run.state != "prepared":
            raise ReleaseAuthorityError("cutover is not awaiting writer fencing")
        fences = self.session.scalars(
            select(rel.LegacyWriterFence).where(rel.LegacyWriterFence.candidate_id == run.candidate_id)
        ).all()
        if not fences or any(fence.state != "verified" for fence in fences):
            raise ReleaseAuthorityError("every planned legacy writer fence must be verified")
        for fence in fences:
            if fence.verified_at is None:
                raise ReleaseAuthorityError("verified writer fence lacks verification chronology")
            _require_at_or_after(
                recorded_at,
                fence.verified_at,
                field="recorded_at",
                floor_field="writer fence verified_at",
            )
        _require_at_or_after(
            recorded_at,
            run.started_at,
            field="recorded_at",
            floor_field="cutover started_at",
        )
        self._require_not_future(recorded_at, "recorded_at")
        self._advance_cutover(run, "fenced")
        self._checkpoint(
            run,
            "legacy_writers_fenced",
            {"fences": [str(fence.fence_id) for fence in fences]},
            recorded_at,
        )
        return run

    def activate_authority(
        self,
        *,
        cutover_run_id: uuid.UUID,
        final_asana_closure_id: uuid.UUID,
        activated_at: datetime,
    ) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        if run.state == "activated":
            return run
        if run.state != "fenced":
            raise ReleaseAuthorityError("authority activation requires verified writer fencing")
        candidate = self._candidate(run.candidate_id)
        closure = self._current_approved_final_asana_closure(
            candidate.candidate_id, expected_closure_id=final_asana_closure_id
        )
        if _utc_comparable(closure.closed_through_at) < _utc_comparable(activated_at):
            raise ReleaseAuthorityError(
                "final Asana closure does not cover the authority activation timestamp"
            )
        fenced_at = self._cutover_checkpoint_time(cutover_run_id, "legacy_writers_fenced")
        _require_at_or_after(
            activated_at,
            fenced_at,
            field="activated_at",
            floor_field="legacy writer fence verification",
        )
        _require_at_or_after(
            activated_at,
            closure.recorded_at,
            field="activated_at",
            floor_field="final closure recorded_at",
        )
        self._require_not_future(activated_at, "activated_at")
        control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
        generation = self.session.get(models.AuthorityGeneration, candidate.generation_id)
        if control is None or control.state != "closed":
            raise ReleaseAuthorityError("mutation admission must remain closed during activation")
        if generation is None or generation.status != "active":
            raise ReleaseAuthorityError("target generation is not active")
        self._advance_cutover(run, "activated")
        self._checkpoint(
            run,
            "authority_activated_admission_closed",
            {
                "generation_id": str(candidate.generation_id),
                "final_asana_closure_id": str(closure.closure_id),
                "final_asana_closure_sha256": closure.closure_sha256,
                "closed_through_at": closure.closed_through_at.isoformat(),
            },
            activated_at,
        )
        return run

    def burn_rollback(
        self,
        *,
        cutover_run_id: uuid.UUID,
        legacy_bundle_id: str,
        burned_at: datetime,
    ) -> models.AuthorityActivation:
        run = self._cutover(cutover_run_id)
        candidate = self._candidate(run.candidate_id)
        approval = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate.candidate_id)
        )
        if run.state == "rollback_burned":
            existing = self.session.scalar(
                select(models.AuthorityActivation).where(
                    models.AuthorityActivation.generation_id == candidate.generation_id,
                    models.AuthorityActivation.outcome == "activated",
                )
            )
            if existing is None:
                raise ReleaseAuthorityError("rollback-burn state lacks activation evidence")
            return existing
        if run.state != "activated" or approval is None:
            raise ReleaseAuthorityError("rollback burn requires activated approved cutover")
        activation_checkpoint = self.session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == "authority_activated_admission_closed",
            )
        )
        if activation_checkpoint is None:
            raise ReleaseAuthorityError("rollback burn lacks final Asana closure activation evidence")
        closure_id_value = activation_checkpoint.payload.get("final_asana_closure_id")
        if closure_id_value is None:
            raise ReleaseAuthorityError("activation checkpoint lacks final Asana closure identity")
        self._current_approved_final_asana_closure(
            candidate.candidate_id, expected_closure_id=uuid.UUID(str(closure_id_value))
        )
        _require_at_or_after(
            burned_at,
            activation_checkpoint.recorded_at,
            field="burned_at",
            floor_field="authority activation",
        )
        self._require_not_future(burned_at, "burned_at")
        batch = self.session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
        if batch is None:
            raise ReleaseAuthorityError("release candidate import batch is missing")
        row = models.AuthorityActivation(
            activation_id=self.uuid_factory(),
            generation_id=candidate.generation_id,
            import_run_id=batch.import_run_id,
            cutover_approval_id=str(approval.approval_id),
            legacy_bundle_id=legacy_bundle_id,
            schema_head=candidate.schema_head,
            dish_release=candidate.dish_release,
            honest_release=candidate.honest_release,
            protocol_release=candidate.protocol_release,
            openapi_release=candidate.openapi_release,
            routing_release=candidate.routing_release,
            projection_epoch=candidate.projection_epoch_id,
            outcome="activated",
            rollback_burned_at=burned_at,
            recorded_at=burned_at,
        )
        self.session.add(row)
        self._advance_cutover(run, "rollback_burned")
        candidate.status = "activated"
        candidate.candidate_revision += 1
        candidate.terminal_at = burned_at
        self._checkpoint(
            run,
            "rollback_burned",
            {"activation_id": str(row.activation_id), "legacy_bundle_id": legacy_bundle_id},
            burned_at,
        )
        self.session.flush()
        return row

    def record_runtime_release_attestation(
        self,
        *,
        candidate_id: uuid.UUID,
        service_artifact_sha256: str,
        projection_worker_artifact_sha256: str,
        route_probe_sha256: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.RuntimeReleaseAttestation:
        candidate = self._candidate(candidate_id)
        cutover = self.session.scalar(
            select(rel.CutoverRun).where(
                rel.CutoverRun.candidate_id == candidate_id,
                rel.CutoverRun.state == "rollback_burned",
            )
        )
        if candidate.status != "activated" or cutover is None:
            raise ReleaseAuthorityError(
                "runtime attestation requires the exact candidate after durable rollback burn"
            )
        burned_at = self._cutover_checkpoint_time(cutover.cutover_run_id, "rollback_burned")
        _require_at_or_after(
            recorded_at,
            burned_at,
            field="recorded_at",
            floor_field="rollback burn",
        )
        self._require_not_future(recorded_at, "recorded_at")
        if not all(
            _is_sha256(value)
            for value in (
                service_artifact_sha256,
                projection_worker_artifact_sha256,
                route_probe_sha256,
            )
        ):
            raise ReleaseAuthorityError("runtime attestation artifact identities must be SHA-256 digests")
        body = dict(payload)
        expected = {
            "dish_release": candidate.dish_release,
            "protocol_release": candidate.protocol_release,
            "openapi_release": candidate.openapi_release,
            "routing_release": candidate.routing_release,
            "route_target": "postgresql",
            "health": "pass",
            "mutation_admission": "closed",
        }
        if any(body.get(key) != value for key, value in expected.items()):
            raise ReleaseAuthorityError("runtime attestation does not match the exact candidate release and closed route")
        identity = {
            "candidate_id": str(candidate_id),
            "service_artifact_sha256": service_artifact_sha256,
            "projection_worker_artifact_sha256": projection_worker_artifact_sha256,
            "route_probe_sha256": route_probe_sha256,
            "payload": body,
        }
        digest = sha256_json(identity)
        existing = self.session.scalar(
            select(rel.RuntimeReleaseAttestation).where(
                rel.RuntimeReleaseAttestation.candidate_id == candidate_id
            )
        )
        if existing is not None:
            if existing.attestation_sha256 != digest:
                raise ReleaseAuthorityError("runtime release attestation identity conflict")
            return existing
        row = rel.RuntimeReleaseAttestation(
            attestation_id=self.uuid_factory(),
            candidate_id=candidate_id,
            service_artifact_sha256=service_artifact_sha256,
            projection_worker_artifact_sha256=projection_worker_artifact_sha256,
            route_probe_sha256=route_probe_sha256,
            payload=body,
            attestation_sha256=digest,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_projection_worker_readiness(
        self,
        *,
        candidate_id: uuid.UUID,
        reconciliation_run_id: uuid.UUID,
        worker_identity: str,
        worker_release: str,
        payload: Mapping[str, Any],
        ready_at: datetime,
    ) -> rel.ProjectionWorkerReadiness:
        candidate = self._candidate(candidate_id)
        cutover = self.session.scalar(
            select(rel.CutoverRun).where(
                rel.CutoverRun.candidate_id == candidate_id,
                rel.CutoverRun.state == "rollback_burned",
            )
        )
        activation = self.session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == candidate.generation_id,
                models.AuthorityActivation.outcome == "activated",
            )
        )
        reconciliation = self.session.get(tx.ProjectionReconciliationRun, reconciliation_run_id)
        body = dict(payload)
        if candidate.status != "activated" or cutover is None or activation is None:
            raise ReleaseAuthorityError(
                "projection worker readiness requires the exact candidate after durable rollback burn"
            )
        _require_at_or_after(
            ready_at,
            activation.rollback_burned_at,
            field="ready_at",
            floor_field="rollback burn",
        )
        self._require_not_future(ready_at, "ready_at")
        if worker_release.strip() != candidate.dish_release:
            raise ReleaseAuthorityError("projection worker readiness is for the wrong release")
        if (
            reconciliation is None
            or reconciliation.generation_id != candidate.generation_id
            or reconciliation.projection_epoch_id != candidate.projection_epoch_id
            or reconciliation.status != "complete"
            or reconciliation.processed_items != reconciliation.expected_items
            or reconciliation.completed_at is None
            or _utc_comparable(reconciliation.completed_at)
            < _utc_comparable(activation.rollback_burned_at)
        ):
            raise ReleaseAuthorityError("projection worker readiness requires complete exact reconciliation")
        if any(body.get(key) != "pass" for key in ("claim_probe", "write_probe", "restart_probe")):
            raise ReleaseAuthorityError("projection worker readiness lacks claim, write, and restart proof")
        identity = {
            "candidate_id": str(candidate_id),
            "projection_epoch_id": str(candidate.projection_epoch_id),
            "reconciliation_run_id": str(reconciliation_run_id),
            "worker_identity": worker_identity,
            "worker_release": worker_release,
            "payload": body,
        }
        digest = sha256_json(identity)
        existing = self.session.scalar(
            select(rel.ProjectionWorkerReadiness).where(
                rel.ProjectionWorkerReadiness.candidate_id == candidate_id
            )
        )
        if existing is not None:
            if existing.readiness_sha256 != digest:
                raise ReleaseAuthorityError("projection worker readiness identity conflict")
            return existing
        row = rel.ProjectionWorkerReadiness(
            readiness_id=self.uuid_factory(),
            candidate_id=candidate_id,
            projection_epoch_id=candidate.projection_epoch_id,
            reconciliation_run_id=reconciliation_run_id,
            worker_identity=worker_identity.strip(),
            worker_release=worker_release.strip(),
            payload=body,
            readiness_sha256=digest,
            ready_at=ready_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def plan_first_admission(
        self,
        *,
        cutover_run_id: uuid.UUID,
        request_id: uuid.UUID,
        command_name: str,
        command_arguments: Mapping[str, Any],
        task_id: uuid.UUID | None,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.FirstAdmissionPlan:
        run = self._cutover(cutover_run_id)
        if run.state != "rollback_burned":
            raise ReleaseAuthorityError(
                "first-admission plan must be recorded after rollback burn and before admission opens"
            )
        burned_at = self._cutover_checkpoint_time(cutover_run_id, "rollback_burned")
        _require_at_or_after(
            recorded_at,
            burned_at,
            field="recorded_at",
            floor_field="rollback burn",
        )
        self._require_not_future(recorded_at, "recorded_at")
        normalized_command = command_name.strip()
        if not normalized_command:
            raise ReleaseAuthorityError("first-admission command must be nonblank")
        try:
            definition = definition_for(normalized_command)
        except ValueError as exc:
            raise ReleaseAuthorityError(str(exc)) from exc
        if not definition.retained or definition.profile == "Q":
            raise ReleaseAuthorityError("first admission must be a retained mutation command")
        arguments = dict(command_arguments)
        expected_projection_events = expected_projection_count(normalized_command, arguments)
        plan_payload = {
            "command_arguments": arguments,
            "operator_evidence": dict(payload),
        }
        body = {
            "cutover_run_id": str(cutover_run_id),
            "request_id": str(request_id),
            "command_name": normalized_command,
            "task_id": None if task_id is None else str(task_id),
            "expected_projection_events": expected_projection_events,
            "payload": plan_payload,
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.FirstAdmissionPlan).where(rel.FirstAdmissionPlan.cutover_run_id == cutover_run_id)
        )
        if existing is not None:
            if existing.plan_sha256 != digest:
                raise ReleaseAuthorityError("first-admission plan identity conflict")
            return existing
        row = rel.FirstAdmissionPlan(
            plan_id=self.uuid_factory(),
            cutover_run_id=cutover_run_id,
            request_id=request_id,
            command_name=normalized_command,
            task_id=task_id,
            expected_projection_events=expected_projection_events,
            payload=plan_payload,
            plan_sha256=digest,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def open_mutation_admission(
        self,
        *,
        cutover_run_id: uuid.UUID,
        opened_at: datetime,
    ) -> rel.MutationAdmissionControl:
        run = self._cutover(cutover_run_id)
        candidate = self._candidate(run.candidate_id)
        control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
        if run.state == "admission_open":
            if control is None or control.state != "open":
                raise ReleaseAuthorityError("cutover state and admission control disagree")
            return control
        if run.state != "rollback_burned" or control is None or control.state != "closed":
            raise ReleaseAuthorityError("mutation admission opens only after durable rollback burn")
        burned_at = self._cutover_checkpoint_time(cutover_run_id, "rollback_burned")
        _require_at_or_after(
            opened_at,
            burned_at,
            field="opened_at",
            floor_field="rollback burn",
        )
        self._require_not_future(opened_at, "opened_at")
        runtime = self.session.scalar(
            select(rel.RuntimeReleaseAttestation).where(
                rel.RuntimeReleaseAttestation.candidate_id == candidate.candidate_id
            )
        )
        worker = self.session.scalar(
            select(rel.ProjectionWorkerReadiness).where(
                rel.ProjectionWorkerReadiness.candidate_id == candidate.candidate_id
            )
        )
        plan = self.session.scalar(
            select(rel.FirstAdmissionPlan).where(rel.FirstAdmissionPlan.cutover_run_id == cutover_run_id)
        )
        if runtime is None or worker is None or plan is None:
            raise ReleaseAuthorityError(
                "mutation admission requires runtime attestation, projection-worker readiness, and first-admission plan"
            )
        if worker.projection_epoch_id != candidate.projection_epoch_id:
            raise ReleaseAuthorityError("projection-worker readiness is for the wrong epoch")
        if (
            _utc_comparable(runtime.recorded_at) > _utc_comparable(opened_at)
            or _utc_comparable(worker.ready_at) > _utc_comparable(opened_at)
            or _utc_comparable(plan.recorded_at) > _utc_comparable(opened_at)
        ):
            raise ReleaseAuthorityError("admission evidence must be durable before admission opens")
        control.state = "open"
        control.control_revision += 1
        control.opened_at = opened_at
        control.updated_at = opened_at
        self._advance_cutover(run, "admission_open")
        self._checkpoint(
            run,
            "mutation_admission_opened",
            {
                "generation_id": str(candidate.generation_id),
                "runtime_attestation_id": str(runtime.attestation_id),
                "projection_worker_readiness_id": str(worker.readiness_id),
                "first_admission_plan_id": str(plan.plan_id),
            },
            opened_at,
        )
        self.session.flush()
        return control

    def verify_first_admission(
        self,
        *,
        cutover_run_id: uuid.UUID,
        request_id: uuid.UUID,
        verified_at: datetime,
    ) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        if run.state == "first_admission_verified":
            return run
        if run.state != "admission_open":
            raise ReleaseAuthorityError("first admission can be verified only after admission opens")
        candidate = self._candidate(run.candidate_id)
        control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
        plan = self.session.scalar(
            select(rel.FirstAdmissionPlan).where(rel.FirstAdmissionPlan.cutover_run_id == cutover_run_id)
        )
        request = self.session.get(wf.ServiceRequest, request_id)
        outcome = self.session.scalar(
            select(wf.ServiceRequestOutcome).where(wf.ServiceRequestOutcome.request_id == request_id)
        )
        execution = self.session.scalar(
            select(wf.CommandExecution).where(wf.CommandExecution.request_id == request_id)
        )
        audit = self.session.scalar(
            select(wf.GovernedAuditEvent).where(wf.GovernedAuditEvent.request_id == request_id)
        )
        obligation = self.session.scalar(
            select(wf.InvocationAuditObligation).where(
                wf.InvocationAuditObligation.request_id == request_id
            )
        )
        projection_events = self.session.scalars(
            select(tx.ProjectionOutboxEvent).where(
                tx.ProjectionOutboxEvent.command_execution_id == (execution.execution_id if execution else None)
            )
        ).all() if execution is not None else []
        reconciliation = (
            self.session.scalar(
                select(tx.ProjectionReconciliationRun)
                .where(
                    tx.ProjectionReconciliationRun.generation_id == candidate.generation_id,
                    tx.ProjectionReconciliationRun.projection_epoch_id == candidate.projection_epoch_id,
                    tx.ProjectionReconciliationRun.status == "complete",
                    tx.ProjectionReconciliationRun.started_at >= request.admitted_at,
                )
                .order_by(tx.ProjectionReconciliationRun.completed_at.desc())
                .limit(1)
            )
            if request is not None
            else None
        )
        active_mapping_ids: set[uuid.UUID] = set()
        for mapping_model in (
            tx.ProjectProjectionMapping,
            tx.SectionProjectionMapping,
            tx.TaskProjectionMapping,
        ):
            active_mapping_ids.update(
                self.session.scalars(
                    select(mapping_model.mapping_id).where(
                        mapping_model.generation_id == candidate.generation_id,
                        mapping_model.projection_epoch_id == candidate.projection_epoch_id,
                        mapping_model.state == "active",
                    )
                ).all()
            )
        reconciliation_items = (
            self.session.scalars(
                select(tx.ProjectionReconciliationItem).where(
                    tx.ProjectionReconciliationItem.reconciliation_run_id
                    == reconciliation.reconciliation_run_id
                )
            ).all()
            if reconciliation is not None
            else []
        )
        reconciled_mapping_ids = {
            item.mapping_id
            for item in reconciliation_items
            if item.mapping_id is not None and item.outcome in {"matched", "reprojected"}
        }
        evidence_times = [
            request.admitted_at if request is not None else None,
            outcome.recorded_at if outcome is not None else None,
            execution.terminal_at if execution is not None else None,
            audit.occurred_at if audit is not None else None,
            obligation.terminal_at if obligation is not None else None,
            reconciliation.completed_at if reconciliation is not None else None,
            *[event.terminal_at for event in projection_events],
            *[item.recorded_at for item in reconciliation_items],
        ]
        chronology_valid = all(
            value is not None
            and _utc_comparable(verified_at) >= _utc_comparable(value)
            for value in evidence_times
        )
        self._require_not_future(verified_at, "verified_at")
        if (
            not chronology_valid
            or control is None
            or control.state != "open"
            or control.opened_at is None
            or plan is None
            or plan.request_id != request_id
            or request is None
            or request.generation_id != candidate.generation_id
            or request.admitted_at < control.opened_at
            or request.command_name != plan.command_name
            or request.canonical_payload.get("arguments")
            != plan.payload.get("command_arguments")
            or outcome is None
            or outcome.outcome_class != "success"
            or not outcome.immutable_success
            or execution is None
            or execution.status != "committed"
            or execution.command_name != plan.command_name
            or execution.task_id != plan.task_id
            or audit is None
            or audit.command_execution_id != execution.execution_id
            or audit.task_id != plan.task_id
            or obligation is None
            or obligation.command_execution_id != execution.execution_id
            or obligation.state not in {"fulfilled", "repaired"}
            or obligation.terminal_at is None
            or len(projection_events) != plan.expected_projection_events
            or any(event.state != "applied" for event in projection_events)
            or reconciliation is None
            or reconciliation.processed_items != reconciliation.expected_items
            or reconciliation.expected_items != len(active_mapping_ids)
            or len(reconciliation_items) != len(active_mapping_ids)
            or reconciled_mapping_ids != active_mapping_ids
        ):
            raise ReleaseAuthorityError(
                "first admission lacks exact committed execution, audit, projection, and reconciliation evidence"
            )
        self._advance_cutover(run, "first_admission_verified")
        self._checkpoint(
            run,
            "first_admission_verified",
            {
                "request_id": str(request_id),
                "outcome_id": str(outcome.outcome_id),
                "execution_id": str(execution.execution_id),
                "audit_event_id": str(audit.audit_event_id),
                "invocation_obligation_id": str(obligation.obligation_id),
                "projection_event_ids": [str(event.projection_event_id) for event in projection_events],
                "reconciliation_run_id": str(reconciliation.reconciliation_run_id),
                "reconciled_mapping_ids": sorted(str(value) for value in active_mapping_ids),
            },
            verified_at,
        )
        return run

    def complete_cutover(self, *, cutover_run_id: uuid.UUID, completed_at: datetime) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        if run.state == "completed":
            return run
        if run.state != "first_admission_verified":
            raise ReleaseAuthorityError("cutover completion requires first-admission verification")
        verified_at = self._cutover_checkpoint_time(cutover_run_id, "first_admission_verified")
        _require_at_or_after(
            completed_at,
            verified_at,
            field="completed_at",
            floor_field="first-admission verification",
        )
        self._require_not_future(completed_at, "completed_at")
        self._advance_cutover(run, "completed", terminal_at=completed_at)
        self._checkpoint(run, "cutover_completed", {}, completed_at)
        return run

    def abort_cutover(
        self,
        *,
        cutover_run_id: uuid.UUID,
        reason: str,
        aborted_at: datetime,
    ) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        candidate = self._candidate(run.candidate_id)
        activation = self.session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == candidate.generation_id,
                models.AuthorityActivation.outcome == "activated",
            )
        )
        if activation is not None or run.state in {
            "rollback_burned",
            "admission_open",
            "first_admission_verified",
            "completed",
        }:
            raise ReleaseAuthorityError("ordinary rollback is prohibited after rollback burn")
        if run.state == "aborted":
            return run
        self._advance_cutover(run, "aborted", terminal_at=aborted_at)
        candidate.status = "aborted"
        candidate.candidate_revision += 1
        candidate.terminal_at = aborted_at
        self._checkpoint(run, "cutover_aborted", {"reason": reason}, aborted_at)
        return run

    def _closure_invalidation(
        self, closure_id: uuid.UUID
    ) -> rel.FinalAsanaClosureInvalidation | None:
        return self.session.scalar(
            select(rel.FinalAsanaClosureInvalidation).where(
                rel.FinalAsanaClosureInvalidation.closure_id == closure_id
            )
        )

    def _valid_final_asana_closure(
        self, candidate_id: uuid.UUID, closure_id: uuid.UUID
    ) -> rel.FinalAsanaClosure:
        closure = self.session.get(rel.FinalAsanaClosure, closure_id)
        if closure is None or closure.candidate_id != candidate_id:
            raise ReleaseAuthorityError("final Asana closure does not belong to candidate")
        if self._closure_invalidation(closure_id) is not None:
            raise ReleaseAuthorityError("final Asana closure was invalidated by an intervening change")
        return closure

    def _current_approved_final_asana_closure(
        self,
        candidate_id: uuid.UUID,
        *,
        expected_closure_id: uuid.UUID | None = None,
    ) -> rel.FinalAsanaClosure:
        approval = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate_id)
        )
        if approval is None:
            raise ReleaseAuthorityError("candidate has no cutover approval")
        recertification = self.session.scalar(
            select(rel.CutoverRecertification)
            .where(rel.CutoverRecertification.candidate_id == candidate_id)
            .order_by(rel.CutoverRecertification.recertification_revision.desc())
            .limit(1)
        )
        if recertification is not None:
            closure_id = recertification.closure_id
        else:
            value = approval.approval_payload.get("final_asana_closure_id")
            if value is None:
                raise ReleaseAuthorityError("cutover approval lacks final Asana closure binding")
            closure_id = uuid.UUID(str(value))
        if expected_closure_id is not None and closure_id != expected_closure_id:
            raise ReleaseAuthorityError("cutover does not bind the currently approved final Asana closure")
        closure = self._valid_final_asana_closure(candidate_id, closure_id)
        if recertification is None:
            expected_sha = approval.approval_payload.get("final_asana_closure_sha256")
            if expected_sha != closure.closure_sha256:
                raise ReleaseAuthorityError("cutover approval final Asana closure digest mismatch")
        return closure

    def _candidate(self, candidate_id: uuid.UUID) -> rel.ReleaseCandidate:
        row = self.session.get(rel.ReleaseCandidate, candidate_id)
        if row is None:
            raise ReleaseAuthorityError("unknown release candidate")
        return row

    def _fence(self, fence_id: uuid.UUID) -> rel.LegacyWriterFence:
        row = self.session.get(rel.LegacyWriterFence, fence_id)
        if row is None:
            raise ReleaseAuthorityError("unknown writer fence")
        return row

    def _cutover(self, cutover_run_id: uuid.UUID) -> rel.CutoverRun:
        row = self.session.get(rel.CutoverRun, cutover_run_id)
        if row is None:
            raise ReleaseAuthorityError("unknown cutover run")
        return row

    def _advance_cutover(
        self,
        run: rel.CutoverRun,
        state: str,
        *,
        terminal_at: datetime | None = None,
    ) -> None:
        run.state = state
        run.state_revision += 1
        run.terminal_at = terminal_at
        self.session.flush()

    def _checkpoint(
        self,
        run: rel.CutoverRun,
        kind: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.CutoverCheckpoint:
        body = dict(payload)
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == run.cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == kind,
            )
        )
        if existing is not None:
            if existing.payload_sha256 != digest:
                raise ReleaseAuthorityError("cutover checkpoint identity conflict")
            return existing
        sequence = int(
            self.session.scalar(
                select(func.coalesce(func.max(rel.CutoverCheckpoint.sequence), 0)).where(
                    rel.CutoverCheckpoint.cutover_run_id == run.cutover_run_id
                )
            )
            or 0
        ) + 1
        row = rel.CutoverCheckpoint(
            checkpoint_id=self.uuid_factory(),
            cutover_run_id=run.cutover_run_id,
            sequence=sequence,
            checkpoint_kind=kind,
            payload=body,
            payload_sha256=digest,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _count(
        self,
        model: Any,
        *conditions: Any,
        join: tuple[Any, Any] | None = None,
        extra: Iterable[Any] = (),
    ) -> int:
        query = select(func.count()).select_from(model)
        if join is not None:
            query = query.join(join[0], join[1])
        query = query.where(*conditions, *tuple(extra))
        return int(self.session.scalar(query) or 0)
