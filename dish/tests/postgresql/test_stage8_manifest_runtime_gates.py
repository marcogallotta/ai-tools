from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

import pytest

from dish_pg.candidate_manifest import revalidate_candidate_manifest
from dish_pg import candidate_manifest_models as manifest_models
from dish_pg.database import session_scope
from dish_pg.release import ReleaseAuthorityError
from dish_pg.release_validation import worker_readiness_report_sha256
from tests.support.postgresql.release import (
    _artifact_file,
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_runtime_and_worker_readiness_report,
    _worker_readiness_probes,
    _writer_fence_proof,
)
from tests.support.postgresql.stage8_cutover_evidence_gates import _burn_rollback
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db


def _plan_first_admission(session, ids, *, service, cutover_run_id, context, task_id, recorded_at):
    run_id = _next(ids)
    _register_run(session, generation_id=context["generation_id"], run_id=run_id)
    service.plan_first_admission(
        cutover_run_id=cutover_run_id,
        request_id=_next(ids),
        command_name="start",
        command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
        task_id=task_id,
        owner_id="owner-1",
        principal_class="agent",
        run_id=run_id,
        payload={"probe": "cc5-first-admission"},
        recorded_at=recorded_at,
    )


def _report_probes(row):
    return {
        "claim": {
            "result": row.claim_probe_result,
            "execution_identity": row.claim_execution_identity,
            "evidence_identity": row.claim_evidence_identity,
        },
        "exact_write": {
            "result": row.exact_write_probe_result,
            "execution_identity": row.exact_write_execution_identity,
            "evidence_identity": row.exact_write_evidence_identity,
        },
        "restart": {
            "result": row.restart_probe_result,
            "execution_identity": row.restart_execution_identity,
            "evidence_identity": row.restart_evidence_identity,
        },
    }


def _rehash(row) -> None:
    row.report_sha256 = worker_readiness_report_sha256(
        candidate_id=row.candidate_id,
        projection_epoch_id=row.projection_epoch_id,
        reconciliation_run_id=row.reconciliation_run_id,
        worker_identity=row.worker_identity,
        worker_release=row.worker_release,
        deployed_artifact_sha256=row.deployed_artifact_sha256,
        probes=_report_probes(row),
        completed_at=row.completed_at,
    )


def test_post_burn_readiness_does_not_stale_approved_manifest(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, _cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        manifest = session.scalar(
            select(manifest_models.ReleaseCandidateManifest).where(
                manifest_models.ReleaseCandidateManifest.candidate_id == candidate_id,
                manifest_models.ReleaseCandidateManifest.manifest_version == 4,
            )
        )
        assert manifest is not None
        approved_fingerprint = manifest.canonical_fingerprint
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity="cc5-post-burn-manifest",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        _record_runtime_and_worker_readiness_report(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=NOW + timedelta(minutes=6),
        )

        revalidation = revalidate_candidate_manifest(
            session,
            uuid_factory=lambda: _next(ids),
            candidate=candidate,
            revalidated_at=NOW + timedelta(minutes=7),
        )
        assert revalidation.result == "matched"
        assert revalidation.observed_fingerprint == approved_fingerprint
        assert session.get(manifest_models.ReleaseCandidateManifest, manifest.manifest_id).canonical_fingerprint == approved_fingerprint


def test_worker_readiness_requires_bound_reconciliation_to_be_completed_first(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, _cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity="cc5-readiness-reconciliation-chronology",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        reconciliation.completed_at = NOW + timedelta(minutes=8)
        session.flush()
        candidate = service.candidate_status(candidate_id)
        service_path, service_sha = _artifact_file("chronology-service-artifact")
        worker_path, worker_sha = _artifact_file("chronology-worker-artifact")
        route_path, route_sha = _artifact_file("chronology-route-probe")
        service.record_runtime_release_attestation(
            candidate_id=candidate_id,
            service_artifact_sha256=service_sha,
            projection_worker_artifact_sha256=worker_sha,
            route_probe_sha256=route_sha,
            payload={
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
                "projection_worker_identity": "projection-worker@chronology",
                "service_artifact_path": service_path,
                "projection_worker_artifact_path": worker_path,
                "route_probe_path": route_path,
            },
            recorded_at=NOW + timedelta(minutes=6),
        )
        with pytest.raises(
            ReleaseAuthorityError,
            match="requires fresh candidate-bound exact reconciliation",
        ):
            service.record_projection_worker_readiness(
                candidate_id=candidate_id,
                reconciliation_run_id=reconciliation.reconciliation_run_id,
                probes=_worker_readiness_probes(
                    worker_identity="projection-worker@chronology"
                ),
                completed_at=NOW + timedelta(minutes=7),
            )


def test_worker_readiness_report_is_immutable_and_probe_set_is_fixed(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, _cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity="cc5-fixed-readiness-probes",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        _runtime, readiness = _record_runtime_and_worker_readiness_report(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=NOW + timedelta(minutes=6),
        )
        probes = _report_probes(readiness)
        probes["caller_defined"] = {
            "result": "pass",
            "execution_identity": "caller:execution",
            "evidence_identity": "caller:evidence",
        }
        with pytest.raises(ReleaseAuthorityError, match="requires exactly claim, exact_write, and restart"):
            service.record_projection_worker_readiness(
                candidate_id=candidate_id,
                reconciliation_run_id=reconciliation.reconciliation_run_id,
                probes=probes,
                completed_at=readiness.completed_at,
            )

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text(
                    "UPDATE projection_worker_readiness SET worker_identity='tampered' "
                    "WHERE readiness_id=:readiness_id"
                ),
                {"readiness_id": readiness.readiness_id.hex},
            )


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


@pytest.mark.parametrize(
    "failure_mode",
    ["missing", "tampered", "wrong-worker", "wrong-artifact", "failed", "stale"],
)
def test_first_admission_ignores_post_burn_worker_readiness_forensics(
    workflow_db, failure_mode: str
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity=f"cc5-first-admission:{failure_mode}",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        _runtime, readiness = _record_runtime_and_worker_readiness_report(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=NOW + timedelta(minutes=6),
        )
        opened_at = NOW + timedelta(minutes=67 if failure_mode == "stale" else 7)
        if failure_mode != "stale":
            session.execute(text("DROP TRIGGER IF EXISTS projection_worker_readiness_immutable_update"))
            session.execute(text("DROP TRIGGER IF EXISTS projection_worker_readiness_immutable_delete"))
        if failure_mode == "missing":
            session.execute(
                text("DELETE FROM projection_worker_readiness WHERE readiness_id=:readiness_id"),
                {"readiness_id": readiness.readiness_id.hex},
            )
        elif failure_mode == "tampered":
            session.execute(
                text("UPDATE projection_worker_readiness SET report_sha256=:digest WHERE readiness_id=:readiness_id"),
                {"digest": "f" * 64, "readiness_id": readiness.readiness_id.hex},
            )
        elif failure_mode in {"wrong-worker", "wrong-artifact", "failed"}:
            if failure_mode == "wrong-worker":
                readiness.worker_identity = "projection-worker@wrong-release-process"
            elif failure_mode == "wrong-artifact":
                readiness.deployed_artifact_sha256 = "f" * 64
            else:
                readiness.claim_probe_result = "fail"
            _rehash(readiness)
            session.execute(
                text(
                    "UPDATE projection_worker_readiness SET worker_identity=:worker_identity, "
                    "deployed_artifact_sha256=:artifact, claim_probe_result=:claim, "
                    "report_sha256=:digest WHERE readiness_id=:readiness_id"
                ),
                {
                    "worker_identity": readiness.worker_identity,
                    "artifact": readiness.deployed_artifact_sha256,
                    "claim": readiness.claim_probe_result,
                    "digest": readiness.report_sha256,
                    "readiness_id": readiness.readiness_id.hex,
                },
            )
        session.expire_all()
        _plan_first_admission(
            session,
            ids,
            service=service,
            cutover_run_id=cutover_run_id,
            context=context,
            task_id=task_id,
            recorded_at=NOW + timedelta(minutes=6),
        )

        control = service.open_mutation_admission(
            cutover_run_id=cutover_run_id,
            opened_at=opened_at,
        )
        assert control.state == "closed"



def test_worker_artifact_substitution_is_forensic_after_burn(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity="cc5-runtime-worker-substitution",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        runtime, _readiness = _record_runtime_and_worker_readiness_report(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=NOW + timedelta(minutes=6),
        )
        _plan_first_admission(
            session,
            ids,
            service=service,
            cutover_run_id=cutover_run_id,
            context=context,
            task_id=task_id,
            recorded_at=NOW + timedelta(minutes=6),
        )
        Path(runtime.payload["projection_worker_artifact_path"]).write_bytes(
            b"substituted-worker-runtime\n"
        )
        control = service.open_mutation_admission(
            cutover_run_id=cutover_run_id,
            opened_at=NOW + timedelta(minutes=7),
        )
        assert control.state == "closed"
