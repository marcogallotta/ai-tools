"""Writer fencing, activation, rollback burn, and first-admission control."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select

from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from . import stage6_models as rel
from .command_contract import definition_for
from .command_effects import expected_projection_count
from .cutover_chronology import _require_at_or_after, _utc_comparable
from .release_evidence import (
    ReleaseAuthorityError,
    _is_sha256,
    _require_nonblank,
    _require_sha256,
    sha256_json,
)


class CutoverControlAuthority:
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
