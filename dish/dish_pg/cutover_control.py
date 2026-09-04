"""Writer fencing, activation, rollback burn, and first-admission control."""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Collection, Mapping

from sqlalchemy import select, text

from dish_service.legacy_writer_fence import (
    LEGACY_WRITER_FENCE_PROBE_PLAN,
    manifest_sha256 as writer_fence_manifest_sha256,
    planned_legacy_writer_fence_manifest,
)

from . import artifact_identity_models as artifact
from . import models
from . import reservation_models as reservations
from . import stage3_models as wf
from . import stage5_models as tx
from . import stage6_models as rel
from .candidate_manifest import revalidate_candidate_manifest
from .command_contract import definition_for
from .cutover_chronology import _require_at_or_after, _utc_comparable
from .release_artifacts import observe_release_artifact
from .release_validation import (
    WORKER_READINESS_REPORT_CONTRACT,
    normalize_worker_readiness_probes,
    validate_reconciliation,
    validate_writer_fence_observation,
    worker_readiness_report_sha256,
)
from .workflow import WorkflowAuthorityError, WorkflowAuthorityService
from .release_evidence import (
    CUTOVER_REHEARSAL_KIND,
    ReleaseAuthorityError,
    _is_sha256,
    _require_nonblank,
    _require_sha256,
    canonical_utc_isoformat,
    sha256_json,
)


class CutoverControlAuthority:
    def _bound_cutover_rehearsal(
        self, run: rel.CutoverRun, *, require_running: bool = True
    ) -> rel.RehearsalRun | None:
        if run.rehearsal_id is None:
            return None
        rehearsal = self.session.get(rel.RehearsalRun, run.rehearsal_id)
        candidate = self._candidate(run.candidate_id)
        if (
            rehearsal is None
            or rehearsal.candidate_id != run.candidate_id
            or rehearsal.rehearsal_kind != CUTOVER_REHEARSAL_KIND
            or rehearsal.environment_identity != candidate.rehearsal_environment_identity
            or rehearsal.source_manifest_sha256 != candidate.source_manifest_sha256
        ):
            raise ReleaseAuthorityError(
                "cutover rehearsal identity is missing, unrelated, or mismatched"
            )
        if require_running and rehearsal.status != "running":
            raise ReleaseAuthorityError("cutover rehearsal is not running")
        return rehearsal

    def _activation_for_cutover(
        self, run: rel.CutoverRun, candidate: rel.ReleaseCandidate
    ) -> models.AuthorityActivation | None:
        conditions = [
            models.AuthorityActivation.generation_id == candidate.generation_id,
            models.AuthorityActivation.outcome == "activated",
        ]
        if run.rehearsal_id is None:
            conditions.append(models.AuthorityActivation.rehearsal_id.is_(None))
        else:
            conditions.append(models.AuthorityActivation.rehearsal_id == run.rehearsal_id)
        return self.session.scalar(select(models.AuthorityActivation).where(*conditions))

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
        plan = dict(manifest)
        unexpected = set(plan) - {"path", "service_release", "probe_plan"}
        if unexpected:
            raise ReleaseAuthorityError(
                "writer fence manifest contains unsupported fields: "
                + ", ".join(sorted(unexpected))
            )
        planned_path = _require_nonblank(plan.get("path"), "manifest.path")
        normalized_path = str(
            Path(os.path.abspath(os.path.normpath(planned_path)))
        )
        if planned_path != normalized_path:
            raise ReleaseAuthorityError(
                "writer fence manifest path must be absolute and normalized"
            )
        service_release = plan.get("service_release", candidate.source_release)
        if service_release != candidate.source_release:
            raise ReleaseAuthorityError(
                "writer fence manifest service_release does not match candidate"
            )
        probe_plan = plan.get("probe_plan", LEGACY_WRITER_FENCE_PROBE_PLAN)
        if probe_plan != LEGACY_WRITER_FENCE_PROBE_PLAN:
            raise ReleaseAuthorityError(
                "writer fence manifest probe_plan does not match the enforced contract"
            )
        existing = self.session.scalar(
            select(rel.LegacyWriterFence).where(
                rel.LegacyWriterFence.candidate_id == candidate_id,
                rel.LegacyWriterFence.target_identity == target_identity,
            )
        )
        fence_id = existing.fence_id if existing is not None else self.uuid_factory()
        planned_manifest = planned_legacy_writer_fence_manifest(
            Path(planned_path),
            fence_id=str(fence_id),
            candidate_id=str(candidate_id),
            source_release=candidate.source_release,
            source_commit=candidate.source_commit,
        )
        digest = writer_fence_manifest_sha256(planned_manifest)
        if existing is not None:
            if existing.manifest_sha256 != digest or existing.mechanism != mechanism:
                raise ReleaseAuthorityError("writer fence identity conflict")
            return existing
        row = rel.LegacyWriterFence(
            fence_id=fence_id,
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

    def record_writer_fence_artifact_observation(
        self,
        *,
        fence_id: uuid.UUID,
        artifact_generation_identity: str,
        canonical_path: str,
        content_sha256: str,
        filesystem_device: int,
        filesystem_inode: int,
        verification_result: str,
        observation_contract_version: str,
        observed_at: datetime,
        recorded_at: datetime,
    ) -> artifact.WriterFenceArtifactObservation:
        row = self._fence(fence_id)
        _require_nonblank(
            artifact_generation_identity, "artifact_generation_identity"
        )
        _require_nonblank(canonical_path, "canonical_path")
        if not canonical_path.startswith("/") or "/../" in canonical_path:
            raise ReleaseAuthorityError("canonical_path must be absolute and normalized")
        _require_sha256(content_sha256, "content_sha256")
        if filesystem_device < 0 or filesystem_inode <= 0:
            raise ReleaseAuthorityError(
                "filesystem device and inode must identify a regular file"
            )
        if verification_result not in {"matched", "mismatched", "unverifiable"}:
            raise ReleaseAuthorityError("unsupported artifact verification_result")
        _require_nonblank(
            observation_contract_version, "observation_contract_version"
        )
        _require_at_or_after(
            recorded_at,
            observed_at,
            field="recorded_at",
            floor_field="observed_at",
        )
        self._require_not_future(observed_at, "observed_at")
        self._require_not_future(recorded_at, "recorded_at")
        evidence_payload = {
            "fence_id": str(row.fence_id),
            "candidate_id": str(row.candidate_id),
            "artifact_generation_identity": artifact_generation_identity,
            "canonical_path": canonical_path,
            "content_sha256": content_sha256,
            "filesystem_device": filesystem_device,
            "filesystem_inode": filesystem_inode,
            "file_type": "regular",
            "regular_file": True,
            "verification_result": verification_result,
            "observation_contract_version": observation_contract_version,
            "observed_at": canonical_utc_isoformat(observed_at),
        }
        digest = sha256_json(evidence_payload)
        existing = self.session.scalar(
            select(artifact.WriterFenceArtifactObservation).where(
                artifact.WriterFenceArtifactObservation.fence_id == fence_id
            )
        )
        if existing is not None:
            expected = (
                row.candidate_id,
                artifact_generation_identity,
                canonical_path,
                content_sha256,
                filesystem_device,
                filesystem_inode,
                verification_result,
                observation_contract_version,
                observed_at,
                recorded_at,
                digest,
            )
            actual = (
                existing.candidate_id,
                existing.artifact_generation_identity,
                existing.canonical_path,
                existing.content_sha256,
                existing.filesystem_device,
                existing.filesystem_inode,
                existing.verification_result,
                existing.observation_contract_version,
                existing.observed_at,
                existing.recorded_at,
                existing.evidence_sha256,
            )
            if actual != expected:
                raise ReleaseAuthorityError(
                    "writer fence artifact observation identity conflict"
                )
            return existing
        observation = artifact.WriterFenceArtifactObservation(
            observation_id=self.uuid_factory(),
            fence_id=row.fence_id,
            candidate_id=row.candidate_id,
            artifact_generation_identity=artifact_generation_identity,
            canonical_path=canonical_path,
            content_sha256=content_sha256,
            filesystem_device=filesystem_device,
            filesystem_inode=filesystem_inode,
            file_type="regular",
            regular_file=True,
            verification_result=verification_result,
            observation_contract_version=observation_contract_version,
            observed_at=observed_at,
            recorded_at=recorded_at,
            evidence_sha256=digest,
        )
        self.session.add(observation)
        self.session.flush()
        return observation

    def engage_writer_fence(
        self,
        *,
        fence_id: uuid.UUID,
        artifact_observation_id: uuid.UUID,
        engaged_at: datetime,
    ) -> rel.LegacyWriterFence:
        row = self._fence(fence_id)
        observation = self.session.get(
            artifact.WriterFenceArtifactObservation, artifact_observation_id
        )
        if observation is None:
            raise ReleaseAuthorityError(
                "writer fence engagement requires durable artifact observation"
            )
        if (
            observation.fence_id != row.fence_id
            or observation.candidate_id != row.candidate_id
            or observation.verification_result != "matched"
            or observation.file_type != "regular"
            or not observation.regular_file
        ):
            raise ReleaseAuthorityError(
                "writer fence artifact observation is not an exact matched regular file"
            )
        if observation.content_sha256 != row.manifest_sha256:
            raise ReleaseAuthorityError(
                "deployed writer fence manifest digest does not match the planned manifest"
            )
        if row.state == "engaged" or row.state == "verified":
            if row.artifact_observation_id != observation.observation_id:
                raise ReleaseAuthorityError(
                    "writer fence engagement artifact identity conflict"
                )
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
        row.artifact_observation_id = observation.observation_id
        row.artifact_verification_result = observation.verification_result
        self.session.flush()
        return row
    def verify_writer_fence(
        self,
        *,
        fence_id: uuid.UUID,
        proof: Mapping[str, Any],
        verified_at: datetime,
        writer_inventory_path: Path | None = None,
        required_writer_inventory: Collection[str] | None = None,
    ) -> rel.LegacyWriterFence:
        row = self._fence(fence_id)
        candidate = self._candidate(row.candidate_id)
        validate_writer_fence_observation(
            self.session,
            fence=row,
            required_writer_inventory=required_writer_inventory,
        )
        body = dict(proof)
        production_writer_inventory = frozenset(
            {"dish-service-prod.service", "dish", "dish-admin"}
        )
        if frozenset(required_writer_inventory or ()) == production_writer_inventory:
            if writer_inventory_path is None:
                raise ReleaseAuthorityError(
                    "production writer fence verification requires the raw writer inventory"
                )
            cutover = self.session.scalar(
                select(rel.CutoverRun).where(
                    rel.CutoverRun.candidate_id == candidate.candidate_id
                )
            )
            if cutover is None:
                raise ReleaseAuthorityError(
                    "writer fence inventory binding requires a prepared cutover run"
                )
            from .operations_evidence import (
                OperationsEvidenceError,
                validate_legacy_writer_inventory,
            )

            try:
                inventory_report = validate_legacy_writer_inventory(
                    inventory_path=writer_inventory_path,
                    expected_candidate_id=str(candidate.candidate_id),
                    expected_cutover_run_id=str(cutover.cutover_run_id),
                    expected_source_commit=str(candidate.source_commit),
                )
            except OperationsEvidenceError as exc:
                raise ReleaseAuthorityError(
                    f"legacy writer inventory validation failed: {exc}"
                ) from exc
            if "legacy_writer_inventory" in body:
                raise ReleaseAuthorityError(
                    "legacy_writer_inventory is authority-owned proof evidence"
                )
            body["legacy_writer_inventory"] = {
                "format": inventory_report["format"],
                "inventory_sha256": inventory_report["inventory_sha256"],
                "report_sha256": inventory_report["report_sha256"],
                "candidate_id": inventory_report["candidate_id"],
                "cutover_run_id": inventory_report["cutover_run_id"],
                "source_commit": inventory_report["source_commit"],
                "writer_count": inventory_report["writer_count"],
            }
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
    def prepare_cutover(
        self,
        *,
        candidate_id: uuid.UUID,
        started_at: datetime,
        rehearsal_id: uuid.UUID | None = None,
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
            if existing.rehearsal_id != rehearsal_id:
                raise ReleaseAuthorityError("cutover rehearsal identity conflict")
            self._bound_cutover_rehearsal(existing)
            return existing
        if rehearsal_id is not None:
            rehearsal = self.session.get(rel.RehearsalRun, rehearsal_id)
            if (
                rehearsal is None
                or rehearsal.candidate_id != candidate_id
                or rehearsal.rehearsal_kind != CUTOVER_REHEARSAL_KIND
                or rehearsal.status != "running"
                or rehearsal.environment_identity != candidate.rehearsal_environment_identity
                or rehearsal.source_manifest_sha256 != candidate.source_manifest_sha256
            ):
                raise ReleaseAuthorityError(
                    "cutover rehearsal identity is missing, unrelated, terminal, or mismatched"
                )
            _require_at_or_after(
                started_at, rehearsal.started_at,
                field="started_at", floor_field="cutover rehearsal started_at",
            )
        row = rel.CutoverRun(
            cutover_run_id=self.uuid_factory(),
            candidate_id=candidate_id,
            rehearsal_id=rehearsal_id,
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
                "rehearsal_id": None if rehearsal_id is None else str(rehearsal_id),
                "final_asana_closure_id": str(approved_closure.closure_id),
                "final_asana_closure_sha256": approved_closure.closure_sha256,
            },
            started_at,
        )
        return row
    def mark_fenced(
        self,
        *,
        cutover_run_id: uuid.UUID,
        recorded_at: datetime,
        required_writer_inventory: Collection[str] | None = None,
    ) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
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
            observation = validate_writer_fence_observation(
                self.session,
                fence=fence,
                required_writer_inventory=required_writer_inventory,
            )
            if fence.artifact_observation_id != observation.observation_id:
                raise ReleaseAuthorityError("writer fence is not bound to its exact persisted observation")
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
        required_writer_inventory: Collection[str] | None = None,
    ) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
        if run.state == "activated":
            return run
        if run.state != "fenced":
            raise ReleaseAuthorityError("authority activation requires verified writer fencing")
        candidate = self._candidate(run.candidate_id)
        self._require_candidate_release_identity(candidate)
        evaluation = self.evaluate_candidate(
            candidate_id=candidate.candidate_id, as_of=activated_at
        )
        if not evaluation.passed:
            raise ReleaseAuthorityError(
                "candidate release gates are no longer satisfied at authority activation"
            )
        revalidation = revalidate_candidate_manifest(
            self.session, uuid_factory=self.uuid_factory, candidate=candidate,
            revalidated_at=activated_at,
        )
        if revalidation.result != "matched":
            raise ReleaseAuthorityError("approved candidate authority manifest is stale")
        for fence in self.session.scalars(
            select(rel.LegacyWriterFence).where(
                rel.LegacyWriterFence.candidate_id == candidate.candidate_id
            )
        ):
            validate_writer_fence_observation(
                self.session,
                fence=fence,
                required_writer_inventory=required_writer_inventory,
            )
        closure = self._current_approved_final_asana_closure(
            candidate.candidate_id, expected_closure_id=final_asana_closure_id
        )
        fenced_at = self._cutover_checkpoint_time(cutover_run_id, "legacy_writers_fenced")
        if _utc_comparable(closure.closed_through_at) < _utc_comparable(fenced_at):
            raise ReleaseAuthorityError(
                "final Asana closure does not cover the legacy writer-fence boundary"
            )
        recertification = self.session.scalar(
            select(rel.CutoverRecertification)
            .where(
                rel.CutoverRecertification.candidate_id == candidate.candidate_id,
                rel.CutoverRecertification.closure_id == closure.closure_id,
            )
            .order_by(rel.CutoverRecertification.recertification_revision.desc())
            .limit(1)
        )
        if recertification is None:
            raise ReleaseAuthorityError(
                "final Asana verification must be recertified after writer fencing"
            )
        _require_at_or_after(
            recertification.recertified_at,
            fenced_at,
            field="recertified_at",
            floor_field="legacy writer fence verification",
        )
        _require_at_or_after(
            activated_at,
            recertification.recertified_at,
            field="activated_at",
            floor_field="final Asana recertification",
        )
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
    def _fence_rollback_burn_state(self) -> None:
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        preparer = bind.dialect.identifier_preparer
        table_names = sorted(
            preparer.quote(table.name)
            for table in models.Base.metadata.tables.values()
        )
        self.session.execute(
            text(f"LOCK TABLE {', '.join(table_names)} IN SHARE MODE")
        )

    def burn_rollback(
        self,
        *,
        cutover_run_id: uuid.UUID,
        legacy_bundle_id: str,
        burned_at: datetime,
        required_writer_inventory: Collection[str] | None = None,
    ) -> models.AuthorityActivation:
        _require_nonblank(legacy_bundle_id, "legacy_bundle_id")
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
        candidate = self._candidate(run.candidate_id)
        if run.state == "rollback_burned":
            self._require_candidate_release_identity(candidate)
            existing = self._activation_for_cutover(run, candidate)
            if existing is None:
                raise ReleaseAuthorityError("rollback-burn state lacks activation evidence")
            if (
                existing.legacy_bundle_id != legacy_bundle_id
                or existing.registry_version_id != candidate.registry_version_id
                or existing.honest_binding_id != candidate.honest_binding_id
                or existing.rollback_burned_at is None
                or _utc_comparable(existing.rollback_burned_at)
                != _utc_comparable(burned_at)
            ):
                raise ReleaseAuthorityError("rollback-burn identity conflict")
            return existing
        if run.state != "activated":
            raise ReleaseAuthorityError("rollback burn requires activated approved cutover")
        activation_checkpoint = self.session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == "authority_activated_admission_closed",
            )
        )
        if activation_checkpoint is None:
            raise ReleaseAuthorityError("rollback burn lacks final Asana closure activation evidence")
        _require_at_or_after(
            burned_at,
            activation_checkpoint.recorded_at,
            field="burned_at",
            floor_field="authority activation",
        )
        self._require_not_future(burned_at, "burned_at")

        self.session.flush()
        self._fence_rollback_burn_state()
        if self.rollback_burn_fence_hook is not None:
            self.rollback_burn_fence_hook()
        self.session.expire_all()
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
        candidate = self._candidate(run.candidate_id)
        if run.state != "activated" or candidate.status != "approved":
            raise ReleaseAuthorityError("rollback burn authority changed while acquiring the burn fence")
        contract = self._require_candidate_release_identity(candidate)
        approval = self.session.scalar(
            select(rel.CutoverApproval).where(
                rel.CutoverApproval.candidate_id == candidate.candidate_id
            )
        )
        if approval is None:
            raise ReleaseAuthorityError("rollback burn requires activated approved cutover")
        activation_checkpoint = self.session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == "authority_activated_admission_closed",
            )
        )
        if activation_checkpoint is None:
            raise ReleaseAuthorityError("rollback burn lacks final Asana closure activation evidence")

        evaluation = self.evaluate_candidate(
            candidate_id=candidate.candidate_id, as_of=burned_at
        )
        if not evaluation.passed:
            failed = ", ".join(check.code for check in evaluation.checks if not check.passed)
            raise ReleaseAuthorityError(
                f"candidate release and quiescence gates failed immediately before rollback burn: {failed}"
            )
        revalidation = revalidate_candidate_manifest(
            self.session,
            uuid_factory=self.uuid_factory,
            candidate=candidate,
            revalidated_at=burned_at,
        )
        if revalidation.result != "matched":
            raise ReleaseAuthorityError("approved candidate authority manifest is stale")
        fences = self.session.scalars(
            select(rel.LegacyWriterFence).where(
                rel.LegacyWriterFence.candidate_id == candidate.candidate_id
            )
        ).all()
        if not fences or any(fence.state != "verified" for fence in fences):
            raise ReleaseAuthorityError("rollback burn requires every legacy writer fence to remain verified")
        for fence in fences:
            validate_writer_fence_observation(
                self.session,
                fence=fence,
                required_writer_inventory=required_writer_inventory,
            )

        closure_id_value = activation_checkpoint.payload.get("final_asana_closure_id")
        if closure_id_value is None:
            raise ReleaseAuthorityError("activation checkpoint lacks final Asana closure identity")
        self._current_approved_final_asana_closure(
            candidate.candidate_id, expected_closure_id=uuid.UUID(str(closure_id_value))
        )
        batch = self.session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
        if batch is None:
            raise ReleaseAuthorityError("release candidate import batch is missing")

        from .transition import ProjectionService

        projection_epoch = self.session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
        if (
            projection_epoch is None
            or projection_epoch.generation_id != candidate.generation_id
            or projection_epoch.status != "active"
        ):
            raise ReleaseAuthorityError(
                "rollback burn requires the candidate projection epoch to be active before external projection is disabled"
            )
        ProjectionService(self.session, uuid_factory=self.uuid_factory).set_external_effects_enabled(
            projection_epoch_id=candidate.projection_epoch_id,
            enabled=False,
            reason="rollback burn ends external Asana projection",
        )

        row = models.AuthorityActivation(
            activation_id=self.uuid_factory(),
            generation_id=candidate.generation_id,
            import_run_id=batch.import_run_id,
            cutover_approval_id=str(approval.approval_id),
            legacy_bundle_id=legacy_bundle_id,
            registry_version_id=contract.registry_version.registry_version_id,
            honest_binding_id=contract.honest_binding.binding_id,
            rehearsal_id=run.rehearsal_id,
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
            {
                "activation_id": str(row.activation_id),
                "rehearsal_id": None if run.rehearsal_id is None else str(run.rehearsal_id),
                "legacy_bundle_id": legacy_bundle_id,
                "registry_version_id": str(contract.registry_version.registry_version_id),
                "honest_binding_id": str(contract.honest_binding.binding_id),
                "fresh_candidate_checks": evaluation.as_dict(),
                "fresh_manifest_revalidation_id": str(revalidation.revalidation_id),
                "writer_fence_ids": [str(fence.fence_id) for fence in fences],
                "external_projection_mode": "disabled_post_burn",
                "projection_epoch_id": str(candidate.projection_epoch_id),
            },
            burned_at,
        )
        self.session.flush()
        return row
    def record_runtime_release_attestation(
        self,
        *,
        candidate_id: uuid.UUID,
        service_artifact_sha256: str,
        route_probe_sha256: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
        projection_worker_artifact_sha256: str | None = None,
    ) -> rel.RuntimeReleaseAttestation:
        candidate = self._candidate(candidate_id)
        contract = self._require_candidate_release_identity(candidate)
        cutover = self.session.scalar(
            select(rel.CutoverRun).where(
                rel.CutoverRun.candidate_id == candidate_id,
                rel.CutoverRun.state == "rollback_burned",
            )
        )
        if cutover is None:
            raise ReleaseAuthorityError(
                "runtime attestation requires the exact candidate after durable rollback burn"
            )
        self._bound_cutover_rehearsal(cutover)
        activation = self._activation_for_cutover(cutover, candidate)
        if (
            activation is None
            or activation.registry_version_id != contract.registry_version.registry_version_id
            or activation.honest_binding_id != contract.honest_binding.binding_id
        ):
            raise ReleaseAuthorityError(
                "runtime attestation activation does not match exact candidate release identity"
            )
        if candidate.status != "activated":
            raise ReleaseAuthorityError(
                "runtime attestation requires the exact candidate after durable rollback burn"
            )
        epoch = self.session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
        if (
            epoch is None
            or epoch.status != "active"
            or epoch.external_effects_enabled
        ):
            raise ReleaseAuthorityError(
                "runtime attestation requires the candidate external projection epoch to be disabled post-burn"
            )
        burned_at = self._cutover_checkpoint_time(cutover.cutover_run_id, "rollback_burned")
        _require_at_or_after(
            recorded_at,
            burned_at,
            field="recorded_at",
            floor_field="rollback burn",
        )
        self._require_not_future(recorded_at, "recorded_at")
        if not _is_sha256(service_artifact_sha256) or not _is_sha256(route_probe_sha256):
            raise ReleaseAuthorityError(
                "runtime attestation service and route artifact identities must be SHA-256 digests"
            )
        if projection_worker_artifact_sha256 is not None and not _is_sha256(
            projection_worker_artifact_sha256
        ):
            raise ReleaseAuthorityError(
                "historical projection-worker artifact identity must be a SHA-256 digest when supplied"
            )
        body = dict(payload)
        expected = {
            "dish_release": candidate.dish_release,
            "honest_release": candidate.honest_release,
            "protocol_release": candidate.protocol_release,
            "registry_version_id": str(candidate.registry_version_id),
            "honest_binding_id": str(candidate.honest_binding_id),
            "openapi_release": candidate.openapi_release,
            "routing_release": candidate.routing_release,
            "route_target": "postgresql",
            "health": "pass",
            "mutation_admission": "closed",
            "external_projection": "disabled_post_burn",
        }
        if any(body.get(key) != value for key, value in expected.items()):
            raise ReleaseAuthorityError(
                "runtime attestation does not match the exact PostgreSQL release, closed route, and disabled external-projection mode"
            )
        artifact_paths = {
            service_artifact_sha256: body.get("service_artifact_path"),
            route_probe_sha256: body.get("route_probe_path"),
        }
        if projection_worker_artifact_sha256 is not None:
            artifact_paths[projection_worker_artifact_sha256] = body.get(
                "projection_worker_artifact_path"
            )
        for digest, artifact_path in artifact_paths.items():
            observe_release_artifact(artifact_path=artifact_path, expected_sha256=digest)
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
        probes: Mapping[str, Any],
        completed_at: datetime,
    ) -> rel.ProjectionWorkerReadiness:
        candidate = self._candidate(candidate_id)
        cutover = self.session.scalar(
            select(rel.CutoverRun).where(
                rel.CutoverRun.candidate_id == candidate_id,
                rel.CutoverRun.state == "rollback_burned",
            )
        )
        if cutover is not None:
            self._bound_cutover_rehearsal(cutover)
        activation = None if cutover is None else self._activation_for_cutover(cutover, candidate)
        runtime = self.session.scalar(
            select(rel.RuntimeReleaseAttestation).where(
                rel.RuntimeReleaseAttestation.candidate_id == candidate_id
            )
        )
        reconciliation = self.session.get(
            tx.ProjectionReconciliationRun, reconciliation_run_id
        )
        if candidate.status != "activated" or cutover is None or activation is None:
            raise ReleaseAuthorityError(
                "projection worker readiness requires the exact candidate after durable rollback burn"
            )
        if runtime is None:
            raise ReleaseAuthorityError(
                "projection worker readiness requires the exact runtime release attestation"
            )
        _require_at_or_after(
            completed_at,
            activation.rollback_burned_at,
            field="completed_at",
            floor_field="rollback burn",
        )
        _require_at_or_after(
            completed_at,
            runtime.recorded_at,
            field="completed_at",
            floor_field="runtime attestation",
        )
        self._require_not_future(completed_at, "completed_at")
        worker_identity = _require_nonblank(
            runtime.payload.get("projection_worker_identity"),
            "runtime attestation projection_worker_identity",
        )
        observe_release_artifact(
            artifact_path=runtime.payload.get("projection_worker_artifact_path"),
            expected_sha256=runtime.projection_worker_artifact_sha256,
        )
        reconciliation_validation = validate_reconciliation(
            self.session, candidate=candidate, as_of=completed_at
        )
        if (
            reconciliation is None
            or reconciliation_validation.run is None
            or reconciliation_validation.run.reconciliation_run_id != reconciliation_run_id
            or not reconciliation_validation.passed
            or reconciliation.completed_at is None
            or _utc_comparable(reconciliation.completed_at)
            < _utc_comparable(activation.rollback_burned_at)
            or _utc_comparable(reconciliation.completed_at)
            > _utc_comparable(completed_at)
        ):
            raise ReleaseAuthorityError(
                "projection worker readiness requires fresh candidate-bound exact reconciliation"
            )
        normalized_probes = normalize_worker_readiness_probes(dict(probes))
        if any(probe["result"] != "pass" for probe in normalized_probes.values()):
            raise ReleaseAuthorityError(
                "projection worker readiness requires every fixed probe to pass"
            )
        report_sha256 = worker_readiness_report_sha256(
            candidate_id=candidate_id,
            projection_epoch_id=candidate.projection_epoch_id,
            reconciliation_run_id=reconciliation_run_id,
            worker_identity=worker_identity,
            worker_release=candidate.dish_release,
            deployed_artifact_sha256=runtime.projection_worker_artifact_sha256,
            probes=normalized_probes,
            completed_at=completed_at,
        )
        existing = self.session.scalar(
            select(rel.ProjectionWorkerReadiness).where(
                rel.ProjectionWorkerReadiness.candidate_id == candidate_id
            )
        )
        if existing is not None:
            if existing.report_sha256 != report_sha256:
                raise ReleaseAuthorityError(
                    "projection worker readiness report identity conflict"
                )
            return existing
        row = rel.ProjectionWorkerReadiness(
            readiness_id=self.uuid_factory(),
            candidate_id=candidate_id,
            projection_epoch_id=candidate.projection_epoch_id,
            reconciliation_run_id=reconciliation_run_id,
            worker_identity=worker_identity,
            worker_release=candidate.dish_release,
            deployed_artifact_sha256=runtime.projection_worker_artifact_sha256,
            report_contract_version=WORKER_READINESS_REPORT_CONTRACT,
            claim_probe_result=normalized_probes["claim"]["result"],
            claim_execution_identity=normalized_probes["claim"]["execution_identity"],
            claim_evidence_identity=normalized_probes["claim"]["evidence_identity"],
            exact_write_probe_result=normalized_probes["exact_write"]["result"],
            exact_write_execution_identity=normalized_probes["exact_write"]["execution_identity"],
            exact_write_evidence_identity=normalized_probes["exact_write"]["evidence_identity"],
            restart_probe_result=normalized_probes["restart"]["result"],
            restart_execution_identity=normalized_probes["restart"]["execution_identity"],
            restart_evidence_identity=normalized_probes["restart"]["evidence_identity"],
            completed_at=completed_at,
            report_sha256=report_sha256,
        )
        self.session.add(row)
        self.session.flush()
        return row
    def _validate_first_admission_targets(
        self,
        *,
        candidate: rel.ReleaseCandidate,
        command_name: str,
        definition: Any,
        arguments: Mapping[str, Any],
        task_id: uuid.UUID | None,
    ) -> uuid.UUID | None:
        if command_name != "start":
            raise ReleaseAuthorityError(
                "first admission must use the bounded start command against an existing task"
            )
        if not definition.task_required:
            raise ReleaseAuthorityError(
                "first admission must bind an existing task before mutation admission opens"
            )
        if task_id is None:
            raise ReleaseAuthorityError("first-admission command requires task_id")
        if "task_gid" in arguments:
            raise ReleaseAuthorityError(
                "first-admission plan must use canonical task_id, not task_gid"
            )
        raw_task_id = arguments.get("task_id")
        if raw_task_id is None:
            raise ReleaseAuthorityError(
                "first-admission command arguments must include canonical task_id"
            )
        try:
            argument_task_id = uuid.UUID(str(raw_task_id))
        except (TypeError, ValueError) as exc:
            raise ReleaseAuthorityError(
                "first-admission command task_id must be a UUID"
            ) from exc
        if argument_task_id != task_id:
            raise ReleaseAuthorityError(
                "first-admission plan task identity conflicts with command arguments"
            )
        task = self.session.get(models.DishTask, task_id)
        head = self.session.get(
            models.DishState, (candidate.generation_id, task_id)
        )
        if task is None or head is None:
            raise ReleaseAuthorityError(
                "first-admission task does not belong to the candidate generation"
            )

        if definition.operation_required:
            raise ReleaseAuthorityError(
                "first-admission command cannot require a pre-existing open operation"
            )
        prohibited_operation_fields = {
            "operation_id",
            "submission_id",
            "prepared_operation_id",
            "target_operation_id",
            "target_cycle_id",
            "intent_challenge_id",
        }
        if prohibited_operation_fields & set(arguments):
            raise ReleaseAuthorityError(
                "first-admission start cannot carry prior operation or challenge identity"
            )
        kind = arguments.get("kind")
        if kind not in {"initial", "change", "verification"}:
            raise ReleaseAuthorityError(
                "first-admission start kind must be initial, change, or verification"
            )
        _require_nonblank(arguments.get("agent"), "first-admission agent")
        if arguments.get("agent") not in {"claude", "gpt", "codex"}:
            raise ReleaseAuthorityError("first-admission agent is unsupported")
        return None

    def plan_first_admission(
        self,
        *,
        cutover_run_id: uuid.UUID,
        request_id: uuid.UUID,
        command_name: str,
        command_arguments: Mapping[str, Any],
        task_id: uuid.UUID | None,
        owner_id: str,
        principal_class: str,
        run_id: uuid.UUID,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.FirstAdmissionPlan:
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
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
        normalized_owner = owner_id.strip()
        if not normalized_owner:
            raise ReleaseAuthorityError("first-admission owner must be nonblank")
        if principal_class not in {"agent", "admin", "verification", "service"}:
            raise ReleaseAuthorityError("first-admission principal class is unsupported")
        candidate = self._candidate(run.candidate_id)
        operation_id = self._validate_first_admission_targets(
            candidate=candidate,
            command_name=normalized_command,
            definition=definition,
            arguments=arguments,
            task_id=task_id,
        )
        try:
            WorkflowAuthorityService(self.session).ensure_initial_cutover_run(
                generation_id=candidate.generation_id,
                run_id=run_id,
                owner_id=normalized_owner,
                agent=str(arguments["agent"]),
                registered_at=recorded_at,
            )
        except WorkflowAuthorityError as exc:
            raise ReleaseAuthorityError(str(exc)) from exc
        expected_projection_events = 0
        canonical_request_payload = {
            "command": normalized_command,
            "arguments": arguments,
            "owner_id": normalized_owner,
            "run_id": str(run_id),
        }
        canonical_payload_sha256 = sha256_json(canonical_request_payload)
        plan_payload = {
            "command_arguments": arguments,
            "operator_evidence": dict(payload),
            "operation_id": None if operation_id is None else str(operation_id),
            "owner_id": normalized_owner,
            "principal_class": principal_class,
            "run_id": str(run_id),
            "canonical_payload_sha256": canonical_payload_sha256,
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
            reservation = self.session.scalar(
                select(reservations.FirstRequestReservation).where(
                    reservations.FirstRequestReservation.plan_id == existing.plan_id,
                    reservations.FirstRequestReservation.cutover_run_id == cutover_run_id,
                    reservations.FirstRequestReservation.candidate_id == candidate.candidate_id,
                    reservations.FirstRequestReservation.generation_id == candidate.generation_id,
                    reservations.FirstRequestReservation.request_id == request_id,
                )
            )
            if (
                reservation is None
                or reservation.state != "reserved"
                or reservation.command_name != normalized_command
                or reservation.owner_id != normalized_owner
                or reservation.principal_class != principal_class
                or reservation.run_id != run_id
                or reservation.canonical_payload_sha256 != canonical_payload_sha256
            ):
                raise ReleaseAuthorityError(
                    "first-admission plan replay lacks its exact active reservation"
                )
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
        reservation = reservations.FirstRequestReservation(
            reservation_id=self.uuid_factory(),
            plan_id=row.plan_id,
            cutover_run_id=cutover_run_id,
            candidate_id=candidate.candidate_id,
            generation_id=candidate.generation_id,
            request_id=request_id,
            command_name=normalized_command,
            owner_id=normalized_owner,
            principal_class=principal_class,
            run_id=run_id,
            canonical_payload_sha256=canonical_payload_sha256,
            state="reserved",
            reservation_revision=1,
            reserved_at=recorded_at,
            consumed_at=None,
        )
        self.session.add(reservation)
        self.session.flush()
        return row
    def open_mutation_admission(
        self,
        *,
        cutover_run_id: uuid.UUID,
        opened_at: datetime,
    ) -> rel.MutationAdmissionControl:
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
        candidate = self._candidate(run.candidate_id)
        control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
        if run.state == "admission_open":
            if control is None or control.state != "closed" or control.opened_at is not None:
                raise ReleaseAuthorityError("cutover state and admission control disagree")
            return control
        if run.state != "rollback_burned" or control is None or control.state != "closed":
            raise ReleaseAuthorityError("first-request admission opens only after durable rollback burn")
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
        plan = self.session.scalar(
            select(rel.FirstAdmissionPlan).where(
                rel.FirstAdmissionPlan.cutover_run_id == cutover_run_id
            )
        )
        reservation = self.session.scalar(
            select(reservations.FirstRequestReservation).where(
                reservations.FirstRequestReservation.cutover_run_id == cutover_run_id,
                reservations.FirstRequestReservation.candidate_id == candidate.candidate_id,
                reservations.FirstRequestReservation.generation_id == candidate.generation_id,
            )
        )
        epoch = self.session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
        if runtime is None or plan is None or reservation is None:
            raise ReleaseAuthorityError(
                "first-request admission requires PostgreSQL runtime attestation, first-admission plan, and reservation"
            )
        if (
            epoch is None
            or epoch.status != "active"
            or epoch.external_effects_enabled
        ):
            raise ReleaseAuthorityError(
                "first-request admission requires durable post-burn external-projection disablement"
            )
        if reservation.state != "reserved":
            raise ReleaseAuthorityError("first-request reservation is not available for admission")
        runtime_paths = {
            runtime.service_artifact_sha256: runtime.payload.get("service_artifact_path"),
            runtime.route_probe_sha256: runtime.payload.get("route_probe_path"),
        }
        for digest, path in runtime_paths.items():
            observe_release_artifact(artifact_path=path, expected_sha256=digest)
        if (
            runtime.payload.get("external_projection") != "disabled_post_burn"
            or _utc_comparable(runtime.recorded_at) > _utc_comparable(opened_at)
            or _utc_comparable(plan.recorded_at) > _utc_comparable(opened_at)
            or _utc_comparable(reservation.reserved_at) > _utc_comparable(opened_at)
        ):
            raise ReleaseAuthorityError(
                "first-request PostgreSQL admission evidence must be durable before the gate opens"
            )
        self._advance_cutover(run, "admission_open")
        self._checkpoint(
            run,
            "first_request_admission_opened",
            {
                "generation_id": str(candidate.generation_id),
                "admission_control_state": control.state,
                "runtime_attestation_id": str(runtime.attestation_id),
                "external_projection_mode": "disabled_post_burn",
                "first_admission_plan_id": str(plan.plan_id),
                "first_request_reservation_id": str(reservation.reservation_id),
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
        self._bound_cutover_rehearsal(run)
        if run.state == "first_admission_verified":
            candidate = self._candidate(run.candidate_id)
            control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
            if control is None or control.state != "open":
                raise ReleaseAuthorityError("verified first admission lacks open mutation admission")
            return run
        if run.state != "admission_open":
            raise ReleaseAuthorityError("first admission can be verified only after admission opens")
        candidate = self._candidate(run.candidate_id)
        control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
        reservation = self.session.scalar(
            select(reservations.FirstRequestReservation).where(
                reservations.FirstRequestReservation.cutover_run_id == cutover_run_id,
                reservations.FirstRequestReservation.candidate_id == candidate.candidate_id,
                reservations.FirstRequestReservation.generation_id == candidate.generation_id,
            )
        )
        first_request_gate_at = self._cutover_checkpoint_time(
            cutover_run_id, "first_request_admission_opened"
        )
        plan = self.session.scalar(
            select(rel.FirstAdmissionPlan).where(
                rel.FirstAdmissionPlan.cutover_run_id == cutover_run_id
            )
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
        planned_operation_id: uuid.UUID | None = None
        if plan is not None:
            raw_planned_operation_id = plan.payload.get("operation_id")
            if raw_planned_operation_id is not None:
                try:
                    planned_operation_id = uuid.UUID(str(raw_planned_operation_id))
                except (TypeError, ValueError):
                    planned_operation_id = uuid.UUID(int=0)

        evidence_times = [
            request.admitted_at if request is not None else None,
            outcome.recorded_at if outcome is not None else None,
            execution.terminal_at if execution is not None else None,
            audit.occurred_at if audit is not None else None,
            obligation.terminal_at if obligation is not None else None,
        ]
        chronology_valid = all(
            value is not None
            and _utc_comparable(verified_at) >= _utc_comparable(value)
            for value in evidence_times
        )
        request_payload_valid = (
            request is not None
            and request.canonical_payload_sha256 == sha256_json(request.canonical_payload)
            and plan is not None
            and request.canonical_payload_sha256
            == plan.payload.get("canonical_payload_sha256")
        )
        outcome_payload_valid = (
            outcome is not None
            and outcome.result_sha256 == sha256_json(outcome.result_payload)
        )
        audit_chain_valid = (
            request is not None
            and outcome is not None
            and execution is not None
            and audit is not None
            and obligation is not None
            and plan is not None
            and audit.generation_id == candidate.generation_id
            and audit.command_execution_id == execution.execution_id
            and audit.task_id == plan.task_id
            and obligation.generation_id == candidate.generation_id
            and obligation.outcome_id == outcome.outcome_id
            and obligation.command_execution_id == execution.execution_id
        )
        self._require_not_future(verified_at, "verified_at")
        if (
            not chronology_valid
            or not request_payload_valid
            or not outcome_payload_valid
            or not audit_chain_valid
            or control is None
            or control.state != "closed"
            or control.opened_at is not None
            or reservation is None
            or reservation.state != "consumed"
            or reservation.request_id != request_id
            or plan is None
            or plan.request_id != request_id
            or plan.expected_projection_events != 0
            or request is None
            or request.generation_id != candidate.generation_id
            or _utc_comparable(request.admitted_at) < _utc_comparable(first_request_gate_at)
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
            or obligation is None
            or obligation.state not in {"fulfilled", "repaired"}
            or obligation.terminal_at is None
        ):
            raise ReleaseAuthorityError(
                "first admission lacks exact PostgreSQL request replay, committed execution, and audit evidence"
            )
        self._advance_cutover(run, "first_admission_verified")
        self.session.flush()
        control.state = "open"
        control.control_revision += 1
        control.opened_at = verified_at
        control.updated_at = verified_at
        self.session.flush()
        self._checkpoint(
            run,
            "first_admission_verified",
            {
                "mutation_admission_state": control.state,
                "mutation_admission_revision": control.control_revision,
                "request_id": str(request_id),
                "request_payload_sha256": request.canonical_payload_sha256,
                "outcome_id": str(outcome.outcome_id),
                "outcome_payload_sha256": outcome.result_sha256,
                "execution_id": str(execution.execution_id),
                "task_id": str(plan.task_id),
                "operation_id": None if planned_operation_id is None else str(planned_operation_id),
                "audit_event_id": str(audit.audit_event_id),
                "invocation_obligation_id": str(obligation.obligation_id),
                "external_projection_mode": "disabled_post_burn",
            },
            verified_at,
        )
        self.session.flush()
        return run
    def complete_cutover(self, *, cutover_run_id: uuid.UUID, completed_at: datetime) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
        if run.state == "completed":
            return run
        if run.state != "first_admission_verified":
            raise ReleaseAuthorityError("cutover completion requires first-admission verification")
        candidate = self._candidate(run.candidate_id)
        verified_at = self._cutover_checkpoint_time(cutover_run_id, "first_admission_verified")
        _require_at_or_after(
            completed_at,
            verified_at,
            field="completed_at",
            floor_field="first-admission verification",
        )
        self._require_not_future(completed_at, "completed_at")
        # Completion is a fresh authority decision, not an inference from prior
        # activation or first-admission evidence. Rebuild and validate the final
        # bundle at the transition boundary so stale, partial, mismatched, or
        # invalidated evidence cannot be carried into the terminal state.
        final_bundle = self.build_evidence_bundle(
            candidate_id=candidate.candidate_id,
            bundle_kind="cutover_final",
            built_at=completed_at,
        )
        manifest_candidate = final_bundle.manifest.get("candidate", {})
        if (
            final_bundle.candidate_id != candidate.candidate_id
            or final_bundle.bundle_kind != "cutover_final"
            or final_bundle.manifest_sha256 != sha256_json(final_bundle.manifest)
            or manifest_candidate.get("candidate_id") != str(candidate.candidate_id)
            or manifest_candidate.get("generation_id") != str(candidate.generation_id)
            or not final_bundle.manifest.get("acceptance", {}).get("passed", False)
        ):
            raise ReleaseAuthorityError(
                "cutover completion requires a current validated final evidence bundle"
            )
        self._advance_cutover(run, "completed", terminal_at=completed_at)
        self._checkpoint(
            run,
            "cutover_completed",
            {
                "final_evidence_bundle_id": str(final_bundle.bundle_id),
                "final_evidence_bundle_sha256": final_bundle.manifest_sha256,
                "candidate_id": str(candidate.candidate_id),
                "generation_id": str(candidate.generation_id),
            },
            completed_at,
        )
        return run
    def teardown_rehearsal_cutover(
        self,
        *,
        cutover_run_id: uuid.UUID,
        rehearsal_id: uuid.UUID,
        reason: str,
        torn_down_at: datetime,
    ) -> rel.CutoverRun:
        reason = _require_nonblank(reason, "reason")
        run = self._cutover(cutover_run_id)
        if run.rehearsal_id is None:
            raise ReleaseAuthorityError("real cutover cannot use rehearsal teardown")
        if run.rehearsal_id != rehearsal_id:
            raise ReleaseAuthorityError("cutover rehearsal identity conflict")
        candidate = self._candidate(run.candidate_id)

        if run.state == "rehearsal_torn_down":
            rehearsal = self._bound_cutover_rehearsal(run, require_running=False)
            checkpoint = self.session.scalar(
                select(rel.CutoverCheckpoint).where(
                    rel.CutoverCheckpoint.cutover_run_id == cutover_run_id,
                    rel.CutoverCheckpoint.checkpoint_kind == "rehearsal_cutover_torn_down",
                )
            )
            activation = self.session.scalar(
                select(models.AuthorityActivation).where(
                    models.AuthorityActivation.generation_id == candidate.generation_id,
                    models.AuthorityActivation.rehearsal_id == rehearsal_id,
                    models.AuthorityActivation.outcome == "aborted",
                )
            )
            reservation = self.session.scalar(
                select(reservations.FirstRequestReservation).where(
                    reservations.FirstRequestReservation.cutover_run_id == cutover_run_id
                )
            )
            control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
            if (
                rehearsal is None
                or rehearsal.status != "failed"
                or run.terminal_at is None
                or _utc_comparable(run.terminal_at) != _utc_comparable(torn_down_at)
                or checkpoint is None
                or checkpoint.payload.get("rehearsal_id") != str(rehearsal_id)
                or checkpoint.payload.get("reason") != reason
                or activation is None
                or candidate.status != "aborted"
                or control is None
                or control.state != "closed"
                or control.opened_at is not None
                or (reservation is not None and reservation.state != "cancelled")
            ):
                raise ReleaseAuthorityError("rehearsal teardown replay identity conflict")
            return run

        rehearsal = self._bound_cutover_rehearsal(run)
        if rehearsal is None:
            raise ReleaseAuthorityError("cutover lacks rehearsal identity")
        if run.state not in {"rollback_burned", "admission_open"}:
            raise ReleaseAuthorityError(
                "rehearsal teardown is allowed only after rollback burn and before first admission"
            )
        activation = self._activation_for_cutover(run, candidate)
        if activation is None or activation.rehearsal_id != rehearsal_id:
            raise ReleaseAuthorityError("rehearsal teardown lacks exact rehearsal activation")
        control = self.session.get(rel.MutationAdmissionControl, candidate.generation_id)
        if (
            candidate.status != "activated"
            or control is None
            or control.candidate_id != candidate.candidate_id
            or control.state != "closed"
            or control.opened_at is not None
        ):
            raise ReleaseAuthorityError(
                "rehearsal teardown requires exact closed candidate admission state"
            )
        reservation = self.session.scalar(
            select(reservations.FirstRequestReservation).where(
                reservations.FirstRequestReservation.cutover_run_id == cutover_run_id,
                reservations.FirstRequestReservation.candidate_id == candidate.candidate_id,
                reservations.FirstRequestReservation.generation_id == candidate.generation_id,
            )
        )
        if run.state == "admission_open" and reservation is None:
            raise ReleaseAuthorityError("rehearsal admission-open state lacks reservation")
        if reservation is not None:
            if reservation.state != "reserved":
                raise ReleaseAuthorityError(
                    "rehearsal teardown requires an unconsumed first-request reservation"
                )
            request = self.session.get(wf.ServiceRequest, reservation.request_id)
            if request is not None:
                raise ReleaseAuthorityError(
                    "rehearsal teardown is prohibited after first-request admission"
                )

        latest_checkpoint_at = self.session.scalar(
            select(rel.CutoverCheckpoint.recorded_at)
            .where(rel.CutoverCheckpoint.cutover_run_id == cutover_run_id)
            .order_by(rel.CutoverCheckpoint.sequence.desc())
            .limit(1)
        )
        _require_at_or_after(
            torn_down_at,
            latest_checkpoint_at or run.started_at,
            field="torn_down_at",
            floor_field="latest cutover checkpoint",
        )
        self._require_not_future(torn_down_at, "torn_down_at")
        prior_state = run.state
        if reservation is not None:
            reservation.state = "cancelled"
            reservation.reservation_revision += 1
        self.session.flush()

        self._advance_cutover(run, "rehearsal_torn_down", terminal_at=torn_down_at)
        activation.outcome = "aborted"
        activation.rollback_burned_at = None
        self.session.flush()
        candidate.status = "aborted"
        candidate.candidate_revision += 1
        candidate.terminal_at = torn_down_at
        self.session.flush()
        checkpoint = self._checkpoint(
            run,
            "rehearsal_cutover_torn_down",
            {
                "rehearsal_id": str(rehearsal_id),
                "reason": reason,
                "prior_state": prior_state,
                "activation_id": str(activation.activation_id),
                "activation_outcome": activation.outcome,
                "mutation_admission_state": control.state,
                "reservation_id": (
                    None if reservation is None else str(reservation.reservation_id)
                ),
                "reservation_state": None if reservation is None else reservation.state,
            },
            torn_down_at,
        )
        rehearsal_report = {
            "rehearsal_kind": CUTOVER_REHEARSAL_KIND,
            "source_manifest_sha256": rehearsal.source_manifest_sha256,
            "environment_identity": rehearsal.environment_identity,
            "result": "failed",
            "checkpoint_manifest_sha256": sha256_json([]),
            "cutover_run_id": str(run.cutover_run_id),
            "teardown_checkpoint_id": str(checkpoint.checkpoint_id),
            "teardown_checkpoint_sha256": checkpoint.payload_sha256,
            "teardown_reason": reason,
        }
        rehearsal.status = "failed"
        rehearsal.run_revision += 1
        rehearsal.report = rehearsal_report
        rehearsal.report_sha256 = sha256_json(rehearsal_report)
        rehearsal.completed_at = torn_down_at
        self.session.flush()
        return run

    def abort_cutover(
        self,
        *,
        cutover_run_id: uuid.UUID,
        reason: str,
        aborted_at: datetime,
    ) -> rel.CutoverRun:
        run = self._cutover(cutover_run_id)
        self._bound_cutover_rehearsal(run)
        candidate = self._candidate(run.candidate_id)
        activation = self._activation_for_cutover(run, candidate)
        if activation is not None or run.state in {
            "rollback_burned",
            "admission_open",
            "first_admission_verified",
            "completed",
        }:
            raise ReleaseAuthorityError("ordinary rollback is prohibited after rollback burn")
        if run.state == "aborted":
            if run.terminal_at is None or _utc_comparable(run.terminal_at) != _utc_comparable(aborted_at):
                raise ReleaseAuthorityError("cutover abort timestamp conflict")
            return run
        latest_checkpoint_at = self.session.scalar(
            select(rel.CutoverCheckpoint.recorded_at)
            .where(rel.CutoverCheckpoint.cutover_run_id == cutover_run_id)
            .order_by(rel.CutoverCheckpoint.sequence.desc())
            .limit(1)
        )
        _require_at_or_after(
            aborted_at, run.started_at,
            field="aborted_at", floor_field="cutover started_at",
        )
        if latest_checkpoint_at is not None:
            _require_at_or_after(
                aborted_at, latest_checkpoint_at,
                field="aborted_at", floor_field="latest cutover checkpoint",
            )
        self._require_not_future(aborted_at, "aborted_at")
        self._advance_cutover(run, "aborted", terminal_at=aborted_at)
        candidate.status = "aborted"
        candidate.candidate_revision += 1
        candidate.terminal_at = aborted_at
        self._checkpoint(run, "cutover_aborted", {"reason": reason}, aborted_at)
        return run
