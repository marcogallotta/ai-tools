from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.release import ReleaseAuthorityError, ReleaseCandidateService
from dish_pg.workflow import (
    ExecutionSpec,
    MutationAdmissionClosed,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityService,
)
from tests.support.postgresql.release import (
    HASH_A,
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_final_closure,
    _writer_fence_proof,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db


def _prepare_approved_cutover(factory, ids, context, task_id):
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        closure = _record_final_closure(
            service, ids, candidate_id, closed_through_at=NOW + timedelta(minutes=5)
        )
        service.approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="Approve this exact candidate and evidence bundle.",
            approval_payload={
                "decision": "approved",
                "final_asana_closure_id": str(closure.closure_id),
                "final_asana_closure_sha256": closure.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=5),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@laptop",
            mechanism="fail-closed-file",
            manifest={"path": "/var/lib/dish/legacy-writer-fence.json"},
            prepared_at=NOW + timedelta(minutes=5),
        )
        cutover = service.prepare_cutover(
            candidate_id=candidate_id, started_at=NOW + timedelta(minutes=5)
        )
        return candidate_id, closure.closure_id, cutover.cutover_run_id, fence.fence_id


def _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id):
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.engage_writer_fence(fence_id=fence_id, engaged_at=NOW + timedelta(minutes=5))
        service.verify_writer_fence(
            fence_id=fence_id,
            proof=_writer_fence_proof(service.writer_fence_status(fence_id), candidate_id),
            verified_at=NOW + timedelta(minutes=5),
        )
        service.mark_fenced(cutover_run_id=cutover_id, recorded_at=NOW + timedelta(minutes=5))
        service.activate_authority(
            cutover_run_id=cutover_id,
            final_asana_closure_id=closure_id,
            activated_at=NOW + timedelta(minutes=5),
        )


def _assert_admission_closed(factory, ids, context, task_id):
    with pytest.raises(MutationAdmissionClosed, match="admission is closed"):
        with session_scope(factory) as session:
            run_id = _next(ids)
            _register_run(session, generation_id=context["generation_id"], run_id=run_id)
            WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)).admit_request(
                RequestSpec(
                    request_id=_next(ids),
                    generation_id=context["generation_id"],
                    run_id=run_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    command_name="start",
                    canonical_payload={"task_id": str(task_id)},
                    protocol_release="protocol-1",
                    dish_release="dish-42619b9",
                    admitted_at=NOW + timedelta(minutes=5),
                )
            )


def _burn_and_open_admission(factory, ids, context, task_id, candidate_id, cutover_id):
    first_request_id = _next(ids)
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        activation = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=NOW + timedelta(minutes=6),
        )
        assert activation.rollback_burned_at is not None
        candidate = service.candidate_status(candidate_id)
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity="projection-worker-readiness",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        service.record_runtime_release_attestation(
            candidate_id=candidate_id,
            service_artifact_sha256="1" * 64,
            projection_worker_artifact_sha256="2" * 64,
            route_probe_sha256="3" * 64,
            payload={
                "dish_release": candidate.dish_release,
                "protocol_release": candidate.protocol_release,
                "openapi_release": candidate.openapi_release,
                "routing_release": candidate.routing_release,
                "route_target": "postgresql",
                "health": "pass",
                "mutation_admission": "closed",
            },
            recorded_at=NOW + timedelta(minutes=6),
        )
        service.record_projection_worker_readiness(
            candidate_id=candidate_id,
            reconciliation_run_id=reconciliation.reconciliation_run_id,
            worker_identity="projection-worker@fixture",
            worker_release=candidate.dish_release,
            payload={"claim_probe": "pass", "write_probe": "pass", "restart_probe": "pass"},
            ready_at=NOW + timedelta(minutes=6),
        )
        service.plan_first_admission(
            cutover_run_id=cutover_id,
            request_id=first_request_id,
            command_name="start",
            command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
            task_id=task_id,
            payload={"probe": "first production mutation"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        control = service.open_mutation_admission(
            cutover_run_id=cutover_id, opened_at=NOW + timedelta(minutes=7)
        )
        assert control.state == "open"
    return first_request_id


def _record_committed_first_request(factory, ids, context, task_id, cutover_id, request_id):
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        workflow.admit_request(
            RequestSpec(
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner_id="owner-1",
                principal_class="agent",
                command_name="start",
                canonical_payload={"arguments": {"task_id": str(task_id), "agent": "codex", "kind": "initial"}},
                protocol_release="protocol-1",
                dish_release="dish-pg-stage6",
                admitted_at=NOW + timedelta(minutes=8),
            )
        )
        binding_id = _next(ids)
        session.add(
            models.HonestContractBinding(
                binding_id=binding_id,
                binding_kind="release",
                source_identity="honest-pantry@stage6-first-admission",
                dish_release="dish-pg-stage6",
                honest_release="honest-1",
                protocol_release="protocol-1",
                protocol_sha256=HASH_A,
                schema_release="schema-1",
                schema_sha256="b" * 64,
                migration_id=None,
                source_schema_version=None,
                target_schema_version=None,
                migration_metadata_sha256=None,
                source_ids={"route": "first-admission"},
                provenance={"source": "cutover-test"},
                resolved_at=NOW + timedelta(minutes=8),
            )
        )
        session.flush()
        execution_id = _next(ids)
        workflow.begin_execution(
            ExecutionSpec(
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                operation_id=None,
                command_name="start",
                transaction_profile="L",
                canonical_intent={"command": "start", "arguments": {"task_id": str(task_id), "agent": "codex", "kind": "initial"}},
                pinned_inputs={"now": (NOW + timedelta(minutes=8)).isoformat()},
                contract_binding_id=binding_id,
                admitted_at=NOW + timedelta(minutes=8),
            )
        )
        workflow.repo.record_outcome(
            request_id=request_id,
            outcome=StoredOutcome(
                outcome_id=_next(ids),
                outcome_class="success",
                result_code="OK",
                http_status=200,
                result_payload={"ok": True, "first_admission": True},
                immutable_success=True,
                recorded_at=NOW + timedelta(minutes=8),
            ),
            execution_id=execution_id,
            audit_event_id=_next(ids),
            audit_event_type="first_production_admission",
            actor="owner-1",
            audit_payload={"cutover_run_id": str(cutover_id)},
            task_id=task_id,
            operation_id=None,
            obligation_id=_next(ids),
            invocation_metadata={"surface": "production-cutover"},
        )
        obligation = session.scalar(
            select(wf.InvocationAuditObligation).where(
                wf.InvocationAuditObligation.request_id == request_id
            )
        )
        assert obligation is not None
        obligation.state = "fulfilled"
        obligation.terminal_at = NOW + timedelta(minutes=8)


def _verify_and_complete(factory, ids, context, candidate_id, cutover_id, request_id):
    with session_scope(factory) as session:
        _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity="post-first-admission",
            started_at=NOW + timedelta(minutes=8),
            completed_at=NOW + timedelta(minutes=9),
        )
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        with pytest.raises(ReleaseAuthorityError, match="execution, audit, projection, and reconciliation"):
            service.verify_first_admission(
                cutover_run_id=cutover_id,
                request_id=request_id,
                verified_at=NOW + timedelta(minutes=8),
            )
        service.verify_first_admission(
            cutover_run_id=cutover_id,
            request_id=request_id,
            verified_at=NOW + timedelta(minutes=9),
        )
        completed = service.complete_cutover(
            cutover_run_id=cutover_id, completed_at=NOW + timedelta(minutes=10)
        )
        assert completed.state == "completed"
        final_bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="cutover_final",
            built_at=NOW + timedelta(minutes=10),
        )
        assert final_bundle.manifest["activation"] is not None
        assert final_bundle.manifest["acceptance"]["passed"], [
            check for check in final_bundle.manifest["acceptance"]["checks"] if not check["passed"]
        ]
        with pytest.raises(ReleaseAuthorityError, match="prohibited"):
            service.abort_cutover(
                cutover_run_id=cutover_id,
                reason="too late",
                aborted_at=NOW + timedelta(minutes=11),
            )


def test_cutover_is_resumable_admission_stays_closed_until_burn_and_first_outcome(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    _assert_admission_closed(factory, ids, context, task_id)
    request_id = _burn_and_open_admission(
        factory, ids, context, task_id, candidate_id, cutover_id
    )
    _record_committed_first_request(factory, ids, context, task_id, cutover_id, request_id)
    _verify_and_complete(factory, ids, context, candidate_id, cutover_id, request_id)
