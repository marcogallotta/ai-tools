from __future__ import annotations
import io
import json
import runpy
from datetime import timedelta
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import (
    ALEMBIC_HEAD,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    ReleaseAuthorityError,
    ReleaseCandidateService,
    sha256_json,
)
from dish_pg.workflow import (
    MutationAdmissionClosed,
    RequestSpec,
    WorkflowAuthorityService,
)
from dish_service.legacy_writer_fence import (
    engage_legacy_writer_fence,
    observe_legacy_writer_fence,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import (
    HASH_A,
    ROOT,
    _complete_active_mapping_reconciliation,
    _record_runtime_and_typed_readiness,
    _seed_worker_probe_inventory,
    _artifact_file,
    _prepare_candidate,
    _record_and_engage_writer_fence,
    _record_final_closure,
    _writer_fence_proof,
)

def _burn_rollback(session, ids, context, task_id):
    service, candidate_id = _prepare_candidate(session, ids, context, task_id)
    bundle = service.build_evidence_bundle(
        candidate_id=candidate_id,
        bundle_kind="release_candidate",
        built_at=NOW,
    )
    service.validate_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        validated_at=NOW + timedelta(minutes=1),
    )
    closure = _record_final_closure(
        service,
        ids,
        candidate_id,
        closed_through_at=NOW + timedelta(minutes=5),
    )
    service.approve_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver="Marco",
        approval_statement="Approve exact candidate for Stage 8 gate tests.",
        approval_payload={
            "final_asana_closure_id": str(closure.closure_id),
            "final_asana_closure_sha256": closure.closure_sha256,
        },
        approved_at=NOW + timedelta(minutes=5),
    )
    fence = service.prepare_writer_fence(
        candidate_id=candidate_id,
        target_identity="legacy-service@stage8-test",
        mechanism="fail-closed-file",
        manifest={"path": "/tmp/stage8-writer-fence.json"},
        prepared_at=NOW + timedelta(minutes=5),
    )
    run = service.prepare_cutover(
        candidate_id=candidate_id,
        started_at=NOW + timedelta(minutes=5),
    )
    _record_and_engage_writer_fence(
        service,
        ids,
        fence_id=fence.fence_id,
        engaged_at=NOW + timedelta(minutes=5),
    )
    service.verify_writer_fence(
        fence_id=fence.fence_id,
        proof=_writer_fence_proof(fence, candidate_id),
        verified_at=NOW + timedelta(minutes=5),
        required_writer_inventory={fence.target_identity},
    )
    service.mark_fenced(
        cutover_run_id=run.cutover_run_id,
        recorded_at=NOW + timedelta(minutes=5),
        required_writer_inventory={fence.target_identity},
    )
    service.recertify_candidate(
        candidate_id=candidate_id,
        closure_id=closure.closure_id,
        approver="Marco",
        recertification_statement="Confirm final closure after writer fencing.",
        payload={"cutover_run_id": str(run.cutover_run_id)},
        recertified_at=NOW + timedelta(minutes=5),
    )
    service.activate_authority(
        cutover_run_id=run.cutover_run_id,
        final_asana_closure_id=closure.closure_id,
        activated_at=NOW + timedelta(minutes=5),
        required_writer_inventory={fence.target_identity},
    )
    service.burn_rollback(
        cutover_run_id=run.cutover_run_id,
        legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
        burned_at=NOW + timedelta(minutes=6),
        required_writer_inventory={fence.target_identity},
    )
    return service, candidate_id, run.cutover_run_id

def _record_runtime_and_worker_readiness(session, ids, service, candidate_id, context):
    reconciliation = _complete_active_mapping_reconciliation(
        session,
        ids,
        candidate_id=candidate_id,
        corpus_identity=f"stage8-readiness:{candidate_id}",
        started_at=NOW + timedelta(minutes=6),
        completed_at=NOW + timedelta(minutes=6),
    )
    return _record_runtime_and_typed_readiness(
        session,
        ids,
        service=service,
        candidate_id=candidate_id,
        reconciliation=reconciliation,
        recorded_at=NOW + timedelta(minutes=6),
        worker_identity="projection-worker@stage8-test",
    )

def _prepare_fenced_recertified_cutover(session, ids, context, task_id):
    service, candidate_id = _prepare_candidate(session, ids, context, task_id)
    bundle = service.build_evidence_bundle(
        candidate_id=candidate_id,
        bundle_kind="release_candidate",
        built_at=NOW,
    )
    service.validate_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        validated_at=NOW + timedelta(minutes=1),
    )
    closure = _record_final_closure(
        service,
        ids,
        candidate_id,
        closed_through_at=NOW + timedelta(minutes=5),
    )
    service.approve_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver="Marco",
        approval_statement="Approve exact candidate for Agent B activation checks.",
        approval_payload={
            "final_asana_closure_id": str(closure.closure_id),
            "final_asana_closure_sha256": closure.closure_sha256,
        },
        approved_at=NOW + timedelta(minutes=5),
    )
    fence = service.prepare_writer_fence(
        candidate_id=candidate_id,
        target_identity="legacy-service@agent-b-activation",
        mechanism="fail-closed-file",
        manifest={"path": "/tmp/agent-b-activation-fence.json"},
        prepared_at=NOW + timedelta(minutes=5),
    )
    run = service.prepare_cutover(
        candidate_id=candidate_id,
        started_at=NOW + timedelta(minutes=5),
    )
    _record_and_engage_writer_fence(
        service,
        ids,
        fence_id=fence.fence_id,
        engaged_at=NOW + timedelta(minutes=5),
    )
    service.verify_writer_fence(
        fence_id=fence.fence_id,
        proof=_writer_fence_proof(fence, candidate_id),
        verified_at=NOW + timedelta(minutes=5),
        required_writer_inventory={fence.target_identity},
    )
    service.mark_fenced(
        cutover_run_id=run.cutover_run_id,
        recorded_at=NOW + timedelta(minutes=5),
        required_writer_inventory={fence.target_identity},
    )
    service.recertify_candidate(
        candidate_id=candidate_id,
        closure_id=closure.closure_id,
        approver="Marco",
        recertification_statement="Recertify exact closure after fencing.",
        payload={"cutover_run_id": str(run.cutover_run_id)},
        recertified_at=NOW + timedelta(minutes=5),
    )
    return service, candidate_id, closure, run, fence


def _case_test_admission_requires_post_burn_runtime_worker_and_first_request_evidence(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        with pytest.raises(ReleaseAuthorityError, match="runtime attestation"):
            service.open_mutation_admission(
                cutover_run_id=cutover_run_id,
                opened_at=NOW + timedelta(minutes=7),
            )

        _record_runtime_and_worker_readiness(
            session, ids, service, candidate_id, context
        )
        with pytest.raises(ReleaseAuthorityError, match="first-admission plan"):
            service.open_mutation_admission(
                cutover_run_id=cutover_run_id,
                opened_at=NOW + timedelta(minutes=7),
            )

        first_request_id = _next(ids)
        first_run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=first_run_id)
        plan = service.plan_first_admission(
            cutover_run_id=cutover_run_id,
            request_id=first_request_id,
            command_name="start",
            command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
            task_id=task_id,
            owner_id="owner-1",
            principal_class="agent",
            run_id=first_run_id,
            payload={"probe": "stage8-first-admission"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        assert plan.expected_projection_events == 0
        assert plan.payload["command_arguments"]["task_id"] == str(task_id)
        canonical_payload = {
            "command": "start",
            "arguments": {
                "task_id": str(task_id),
                "agent": "codex",
                "kind": "initial",
            },
            "owner_id": "owner-1",
            "run_id": str(first_run_id),
        }
        with pytest.raises(
            IntegrityError,
            match="mutation admission is closed",
        ), session.begin_nested():
            session.execute(
                text(
                    """INSERT INTO service_requests (
                        request_id,generation_id,run_id,owner_id,principal_class,
                        command_name,canonical_payload_sha256,canonical_payload,
                        protocol_release,dish_release,admitted_at
                    ) VALUES (
                        :request_id,:generation_id,:run_id,'owner-1','agent','start',
                        :payload_sha,:payload,'protocol-1','dish-pg-stage6',:admitted_at
                    )"""
                ),
                {
                    "request_id": first_request_id.hex,
                    "generation_id": context["generation_id"].hex,
                    "run_id": first_run_id.hex,
                    "payload_sha": sha256_json(canonical_payload),
                    "payload": json.dumps(canonical_payload),
                    "admitted_at": NOW + timedelta(minutes=6),
                },
            )
            session.flush()
        with pytest.raises(
            MutationAdmissionClosed,
            match="pending first-request gate",
        ):
            WorkflowAuthorityService(session).admit_request(
                RequestSpec(
                    request_id=first_request_id,
                    generation_id=context["generation_id"],
                    run_id=first_run_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    command_name="start",
                    canonical_payload=canonical_payload,
                    protocol_release="protocol-1",
                    dish_release="dish-pg-stage6",
                    admitted_at=NOW + timedelta(minutes=6),
                )
            )
        control = service.open_mutation_admission(
            cutover_run_id=cutover_run_id,
            opened_at=NOW + timedelta(minutes=7),
        )
        assert control.state == "closed"
        checkpoint = session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == "first_request_admission_opened",
            )
        )
        assert checkpoint is not None
        assert checkpoint.payload["runtime_attestation_id"]
        assert checkpoint.payload["projection_worker_readiness_id"]
        assert checkpoint.payload["first_admission_plan_id"]
