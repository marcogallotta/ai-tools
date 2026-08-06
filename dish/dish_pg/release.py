"""Stage 6 release-candidate validation, evidence, and cutover control.

All methods participate in the caller-owned SQLAlchemy transaction.  This module
performs no network I/O and never fabricates environment evidence: production
snapshots, restore measurements, fence probes, and Marco approval are supplied
as exact, digest-bound inputs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from . import stage6_models as rel
from .cutover_chronology import _require_at_or_after, _require_aware, _utc_comparable
from .cutover_control import CutoverControlAuthority
from .final_asana_closure import FinalAsanaClosureAuthority
from .release_artifacts import observe_release_artifact
from .release_validation import (
    validate_reconciliation,
    validate_typed_import_linkage,
)
from .release_evidence import (
    EVIDENCE_ARTIFACT_KINDS,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    REQUIRED_EVIDENCE,
    REQUIRED_REHEARSAL_CHECKPOINTS,
    REQUIRED_REHEARSALS,
    ReleaseAuthorityError,
    _is_sha256,
    _require_nonblank,
    _require_sha256,
    _validate_checkpoint_payload,
    _validate_evidence_payload,
    canonical_json,
    sha256_json,
)
from .release_status import (
    AcceptanceCheck,
    CandidateEvaluation,
    ReleaseCandidateStatus,
    WriterFenceStatus,
)

ALEMBIC_HEAD = "0030_validation_failure_admission"


class ReleaseCandidateService(
    FinalAsanaClosureAuthority, CutoverControlAuthority
):
    def __init__(
        self,
        session: Session,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        clock: Callable[[], datetime] | None = None,
        rollback_burn_fence_hook: Callable[[], None] | None = None,
    ) -> None:
        self.session = session
        self.uuid_factory = uuid_factory
        self.clock = clock
        self.rollback_burn_fence_hook = rollback_burn_fence_hook

    def _trusted_now(self) -> datetime:
        value = self.clock() if self.clock is not None else self.session.scalar(
            select(func.current_timestamp())
        )
        if not isinstance(value, datetime):
            raise ReleaseAuthorityError("database clock did not return a timestamp")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _require_not_future(self, value: datetime, field: str) -> None:
        _require_aware(value, field)
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
        self._require_not_future(created_at, "created_at")
        if existing is not None:
            if (
                any(getattr(existing, key) != value for key, value in identity.items())
                or _utc_comparable(existing.created_at) != _utc_comparable(created_at)
            ):
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
        chronology_floors = {
            "active generation creation": generation.created_at,
            "source import completion": batch.completed_at,
            "shadow baseline closure": baseline.terminal_at,
            "projection epoch creation": epoch.created_at,
        }
        for floor_field, floor in chronology_floors.items():
            if floor is None:
                raise ReleaseAuthorityError(
                    f"release candidate lacks {floor_field} chronology"
                )
            _require_at_or_after(
                created_at, floor, field="created_at", floor_field=floor_field
            )

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
        control = self.session.get(
            rel.MutationAdmissionControl, generation_id, with_for_update=True
        )
        if control is None:
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
        else:
            prior_candidate = self.session.get(rel.ReleaseCandidate, control.candidate_id)
            activation = self.session.scalar(
                select(models.AuthorityActivation).where(
                    models.AuthorityActivation.generation_id == generation_id,
                    models.AuthorityActivation.outcome == "activated",
                )
            )
            if (
                prior_candidate is None
                or prior_candidate.status != "aborted"
                or control.state != "closed"
                or activation is not None
            ):
                raise ReleaseAuthorityError(
                    "generation admission control can be rebound only after an exact pre-burn abort"
                )
            control.candidate_id = candidate_id
            control.control_revision += 1
            control.opened_at = None
            control.updated_at = created_at
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
        _require_at_or_after(
            recorded_at, candidate.created_at,
            field="recorded_at", floor_field="candidate created_at",
        )
        self._require_not_future(recorded_at, "recorded_at")
        body = _validate_evidence_payload(
            category=category,
            evidence_key=evidence_key,
            outcome=outcome,
            payload=payload,
        )
        observation = observe_release_artifact(
            artifact_path=body["artifact_path"],
            expected_sha256=body["artifact_sha256"],
        )
        if observation.mtime_ns < int(_utc_comparable(candidate.created_at).timestamp() * 1_000_000_000):
            raise ReleaseAuthorityError("release evidence artifact predates the candidate and is stale")
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
        if latest is not None:
            if latest.payload_sha256 == digest and latest.outcome == outcome:
                if _utc_comparable(latest.recorded_at) != _utc_comparable(recorded_at):
                    raise ReleaseAuthorityError("release evidence replay timestamp conflict")
                return latest
            _require_at_or_after(
                recorded_at, latest.recorded_at,
                field="recorded_at", floor_field="prior evidence revision",
            )
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
        _require_at_or_after(
            started_at, candidate.created_at,
            field="started_at", floor_field="candidate created_at",
        )
        self._require_not_future(started_at, "started_at")
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
            if _utc_comparable(existing.started_at) != _utc_comparable(started_at):
                raise ReleaseAuthorityError("rehearsal start timestamp conflict")
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
        _require_at_or_after(
            recorded_at, rehearsal.started_at,
            field="recorded_at", floor_field="rehearsal started_at",
        )
        self._require_not_future(recorded_at, "recorded_at")
        latest_checkpoint = self.session.scalar(
            select(rel.RehearsalCheckpoint)
            .where(rel.RehearsalCheckpoint.rehearsal_id == rehearsal_id)
            .order_by(rel.RehearsalCheckpoint.sequence.desc())
            .limit(1)
        )
        if latest_checkpoint is not None:
            _require_at_or_after(
                recorded_at, latest_checkpoint.recorded_at,
                field="recorded_at", floor_field="prior rehearsal checkpoint",
            )
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
        observation = observe_release_artifact(
            artifact_path=body["artifact_path"],
            expected_sha256=body["artifact_sha256"],
        )
        if observation.mtime_ns < int(_utc_comparable(rehearsal.started_at).timestamp() * 1_000_000_000):
            raise ReleaseAuthorityError("rehearsal checkpoint artifact predates the rehearsal and is stale")
        digest = sha256_json(body)
        if existing is not None:
            if (
                existing.payload_sha256 != digest
                or _utc_comparable(existing.recorded_at) != _utc_comparable(recorded_at)
            ):
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
        _require_at_or_after(
            completed_at, row.started_at,
            field="completed_at", floor_field="rehearsal started_at",
        )
        for checkpoint in checkpoints:
            _require_at_or_after(
                completed_at, checkpoint.recorded_at,
                field="completed_at", floor_field=f"checkpoint {checkpoint.checkpoint_kind}",
            )
        self._require_not_future(completed_at, "completed_at")
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
            if (
                row.status != terminal
                or row.report_sha256 != digest
                or row.completed_at is None
                or _utc_comparable(row.completed_at) != _utc_comparable(completed_at)
                or row.measured_rpo_seconds != measured_rpo_seconds
                or row.measured_rto_seconds != measured_rto_seconds
            ):
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





    def evaluate_candidate(
        self, *, candidate_id: uuid.UUID, as_of: datetime | None = None
    ) -> CandidateEvaluation:
        candidate = self._candidate(candidate_id)
        evaluation_time = candidate.created_at if as_of is None else as_of
        _require_at_or_after(
            evaluation_time, candidate.created_at,
            field="evaluation_time", floor_field="candidate created_at",
        )
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

        typed_import_ok, typed_import_details = validate_typed_import_linkage(
            self.session, candidate=candidate
        )
        add("typed_import_linkage_exact", typed_import_ok, **typed_import_details)

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
                wf.PlanningIntentChallenge.state.in_(("issued", "claimed", "consumed")),
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
        reconciliation = validate_reconciliation(
            self.session, candidate=candidate, as_of=evaluation_time
        )
        add(
            "projection_ready",
            not any(projection_counts.values()) and reconciliation.passed,
            **projection_counts,
            **reconciliation.details,
        )
        quiescent_counts = {
            **{f"authority_{key}": value for key, value in open_counts.items()},
            **{f"projection_{key}": value for key, value in projection_counts.items()},
        }
        add(
            "quiescent_cutover_authority",
            not any(quiescent_counts.values()),
            **quiescent_counts,
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
        evidence_status: dict[str, str | None] = {}
        artifact_errors: dict[str, str] = {}
        for category, key in REQUIRED_EVIDENCE:
            item = latest_evidence.get((category, key))
            identity = f"{category}:{key}"
            evidence_status[identity] = None if item is None else item.outcome
            if item is not None:
                try:
                    observe_release_artifact(
                        artifact_path=item.payload.get("artifact_path"),
                        expected_sha256=item.payload.get("artifact_sha256"),
                    )
                except ReleaseAuthorityError as exc:
                    artifact_errors[identity] = str(exc)
        add(
            "required_acceptance_evidence",
            all(value == "pass" for value in evidence_status.values())
            and not artifact_errors,
            evidence=evidence_status,
            artifact_errors=artifact_errors,
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
        rehearsal_artifact_errors: dict[str, str] = {}
        for checkpoint in self.session.scalars(
            select(rel.RehearsalCheckpoint)
            .join(rel.RehearsalRun, rel.RehearsalRun.rehearsal_id == rel.RehearsalCheckpoint.rehearsal_id)
            .where(rel.RehearsalRun.candidate_id == candidate_id)
        ):
            try:
                observe_release_artifact(
                    artifact_path=checkpoint.payload.get("artifact_path"),
                    expected_sha256=checkpoint.payload.get("artifact_sha256"),
                )
            except ReleaseAuthorityError as exc:
                rehearsal_artifact_errors[f"{checkpoint.rehearsal_id}:{checkpoint.checkpoint_kind}"] = str(exc)
        add(
            "required_rehearsals",
            all(value == "passed" for value in rehearsal_status.values())
            and not rehearsal_artifact_errors,
            rehearsals=rehearsal_status,
            artifact_errors=rehearsal_artifact_errors,
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
        _require_at_or_after(
            built_at, candidate.created_at,
            field="built_at", floor_field="candidate created_at",
        )
        self._require_not_future(built_at, "built_at")
        latest_evidence_at = self.session.scalar(
            select(func.max(rel.ReleaseEvidenceItem.recorded_at)).where(
                rel.ReleaseEvidenceItem.candidate_id == candidate_id
            )
        )
        latest_rehearsal_at = self.session.scalar(
            select(func.max(rel.RehearsalRun.completed_at)).where(
                rel.RehearsalRun.candidate_id == candidate_id,
                rel.RehearsalRun.completed_at.is_not(None),
            )
        )
        for floor_field, floor in (
            ("latest release evidence", latest_evidence_at),
            ("latest rehearsal completion", latest_rehearsal_at),
        ):
            if floor is not None:
                _require_at_or_after(
                    built_at, floor, field="built_at", floor_field=floor_field
                )
        evaluation = self.evaluate_candidate(candidate_id=candidate_id, as_of=built_at)
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
        _require_at_or_after(
            validated_at, candidate.created_at,
            field="validated_at", floor_field="candidate created_at",
        )
        _require_at_or_after(
            validated_at, bundle.built_at,
            field="validated_at", floor_field="evidence bundle built_at",
        )
        self._require_not_future(validated_at, "validated_at")
        for floor_field, floor in (
            (
                "latest release evidence",
                self.session.scalar(
                    select(func.max(rel.ReleaseEvidenceItem.recorded_at)).where(
                        rel.ReleaseEvidenceItem.candidate_id == candidate_id
                    )
                ),
            ),
            (
                "latest rehearsal completion",
                self.session.scalar(
                    select(func.max(rel.RehearsalRun.completed_at)).where(
                        rel.RehearsalRun.candidate_id == candidate_id,
                        rel.RehearsalRun.completed_at.is_not(None),
                    )
                ),
            ),
        ):
            if floor is not None:
                _require_at_or_after(
                    validated_at, floor, field="validated_at", floor_field=floor_field
                )
        evaluation = self.evaluate_candidate(candidate_id=candidate_id, as_of=validated_at)
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



















    def candidate_status(self, candidate_id: uuid.UUID) -> ReleaseCandidateStatus:
        row = self._candidate(candidate_id)
        return ReleaseCandidateStatus(
            candidate_id=row.candidate_id,
            generation_id=row.generation_id,
            projection_epoch_id=row.projection_epoch_id,
            source_release=row.source_release,
            source_commit=row.source_commit,
            dish_release=row.dish_release,
            protocol_release=row.protocol_release,
            openapi_release=row.openapi_release,
            routing_release=row.routing_release,
            status=row.status,
        )

    def writer_fence_status(self, fence_id: uuid.UUID) -> WriterFenceStatus:
        row = self._fence(fence_id)
        return WriterFenceStatus(
            fence_id=row.fence_id,
            candidate_id=row.candidate_id,
            target_identity=row.target_identity,
            manifest_sha256=row.manifest_sha256,
            state=row.state,
            proof_sha256=row.proof_sha256,
        )

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
