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

from tests.support.postgresql.stage8_cutover_evidence_gates import (
    _burn_rollback,
    _record_runtime_and_worker_readiness,
    _prepare_fenced_recertified_cutover,
)


def test_activation_revalidates_the_exact_approved_candidate_manifest(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, closure, run, fence = _prepare_fenced_recertified_cutover(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        _seed_worker_probe_inventory(
            session,
            ids,
            candidate=candidate,
            sealed_at=NOW + timedelta(minutes=5),
        )

        with pytest.raises(ReleaseAuthorityError, match="authority manifest is stale"):
            service.activate_authority(
                cutover_run_id=run.cutover_run_id,
                final_asana_closure_id=closure.closure_id,
                activated_at=NOW + timedelta(minutes=5),
                required_writer_inventory={fence.target_identity},
            )
        assert service._cutover(run.cutover_run_id).state == "fenced"

def test_writer_fence_verification_requires_exact_persisted_observation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@missing-observation",
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/missing-observation.json"},
            prepared_at=NOW,
        )
        with pytest.raises(ReleaseAuthorityError, match="lacks the artifact observation"):
            service.verify_writer_fence(
                fence_id=fence.fence_id,
                proof=_writer_fence_proof(fence, candidate_id),
                verified_at=NOW + timedelta(minutes=1),
                required_writer_inventory={fence.target_identity},
            )
        assert service.writer_fence_status(fence.fence_id).state == "prepared"

def test_caller_pass_strings_cannot_replace_typed_worker_probe_evidence(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity="agent-b-untyped-readiness",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        service_path, service_sha = _artifact_file("untyped-service")
        worker_path, worker_sha = _artifact_file("untyped-worker")
        route_path, route_sha = _artifact_file("untyped-route")
        service.record_runtime_release_attestation(
            candidate_id=candidate_id,
            service_artifact_sha256=service_sha,
            projection_worker_artifact_sha256=worker_sha,
            route_probe_sha256=route_sha,
            payload={
                "dish_release": candidate.dish_release,
                "protocol_release": candidate.protocol_release,
                "openapi_release": candidate.openapi_release,
                "routing_release": candidate.routing_release,
                "route_target": "postgresql",
                "health": "pass",
                "mutation_admission": "closed",
                "service_artifact_path": service_path,
                "projection_worker_artifact_path": worker_path,
                "route_probe_path": route_path,
            },
            recorded_at=NOW + timedelta(minutes=6),
        )
        _seed_worker_probe_inventory(
            session,
            ids,
            candidate=candidate,
            sealed_at=NOW + timedelta(minutes=6),
        )
        service.record_projection_worker_readiness(
            candidate_id=candidate_id,
            reconciliation_run_id=reconciliation.reconciliation_run_id,
            worker_identity="projection-worker@untyped",
            worker_release=candidate.dish_release,
            payload={"claim_probe": "pass", "write_probe": "pass", "restart_probe": "pass"},
            ready_at=NOW + timedelta(minutes=6),
        )
        first_run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=first_run_id)
        service.plan_first_admission(
            cutover_run_id=cutover_run_id,
            request_id=_next(ids),
            command_name="start",
            command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
            task_id=task_id,
            owner_id="owner-1",
            principal_class="agent",
            run_id=first_run_id,
            payload={"probe": "untyped-readiness-must-fail"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        with pytest.raises(ReleaseAuthorityError, match="completed typed probe evidence"):
            service.open_mutation_admission(
                cutover_run_id=cutover_run_id,
                opened_at=NOW + timedelta(minutes=7),
            )

def test_runtime_artifact_substitution_blocks_admission_after_typed_readiness(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity="agent-b-runtime-substitution",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        runtime, _readiness = _record_runtime_and_typed_readiness(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=NOW + timedelta(minutes=6),
        )
        first_run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=first_run_id)
        service.plan_first_admission(
            cutover_run_id=cutover_run_id,
            request_id=_next(ids),
            command_name="start",
            command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
            task_id=task_id,
            owner_id="owner-1",
            principal_class="agent",
            run_id=first_run_id,
            payload={"probe": "runtime-substitution-must-fail"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        Path(runtime.payload["service_artifact_path"]).write_bytes(b"substituted-runtime\n")
        with pytest.raises(ReleaseAuthorityError, match="digest does not match"):
            service.open_mutation_admission(
                cutover_run_id=cutover_run_id,
                opened_at=NOW + timedelta(minutes=7),
            )
