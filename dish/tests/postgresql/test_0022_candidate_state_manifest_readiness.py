"""Readiness inventory and completion are part of the candidate manifest."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dish_pg import readiness_evidence_models as readiness
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from tests.support.postgresql.candidate_manifest import (
    _approve,
    _revalidate,
    _validated_candidate,
)
from tests.support.postgresql.release import HASH_A
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _seed_inventory(session, ids, candidate):
    inventory = readiness.WorkerProbeInventory(
        inventory_id=_next(ids),
        candidate_id=candidate.candidate_id,
        projection_epoch_id=candidate.projection_epoch_id,
        inventory_version=1,
        required_probe_count=3,
        inventory_sha256=HASH_A,
        inventory_contract_version="worker-probes-v1",
        sealed_at=NOW,
    )
    session.add(inventory)
    session.flush()
    requirements = []
    for ordinal, probe_kind in enumerate(("claim", "write", "restart")):
        row = readiness.WorkerProbeRequirement(
            requirement_id=_next(ids),
            inventory_id=inventory.inventory_id,
            probe_kind=probe_kind,
            ordinal=ordinal,
            probe_contract_version="probe-v1",
        )
        session.add(row)
        requirements.append(row)
    session.flush()
    return inventory, requirements


def _seed_readiness_completion(session, ids, candidate, inventory, requirements):
    reconciliation = session.scalar(
        select(tx.ProjectionReconciliationRun).where(
            tx.ProjectionReconciliationRun.generation_id == candidate.generation_id,
            tx.ProjectionReconciliationRun.projection_epoch_id
            == candidate.projection_epoch_id,
        )
    )
    assert reconciliation is not None
    worker = rel.ProjectionWorkerReadiness(
        readiness_id=_next(ids),
        candidate_id=candidate.candidate_id,
        projection_epoch_id=candidate.projection_epoch_id,
        reconciliation_run_id=reconciliation.reconciliation_run_id,
        probe_inventory_id=inventory.inventory_id,
        worker_identity="projection-worker@artifact",
        worker_release=candidate.dish_release,
        payload={"claim_probe": "pass", "write_probe": "pass", "restart_probe": "pass"},
        readiness_sha256="b" * 64,
        ready_at=NOW,
    )
    session.add(worker)
    session.flush()
    for requirement in requirements:
        session.add(
            readiness.WorkerProbeEvidence(
                evidence_id=_next(ids),
                readiness_id=worker.readiness_id,
                requirement_id=requirement.requirement_id,
                inventory_id=inventory.inventory_id,
                candidate_id=candidate.candidate_id,
                projection_epoch_id=candidate.projection_epoch_id,
                probe_kind=requirement.probe_kind,
                execution_identity=f"rehearsal:{requirement.probe_kind}",
                worker_identity=worker.worker_identity,
                deployed_artifact_sha256="c" * 64,
                result="pass",
                observed_at=NOW,
                evidence_artifact_identity=f"fixture:{requirement.probe_kind}",
                evidence_sha256="d" * 64,
                recorded_at=NOW,
            )
        )
    session.flush()
    completion = readiness.WorkerReadinessCompletion(
        completion_id=_next(ids),
        readiness_id=worker.readiness_id,
        inventory_id=inventory.inventory_id,
        candidate_id=candidate.candidate_id,
        projection_epoch_id=candidate.projection_epoch_id,
        completion_state="complete",
        required_probe_count=3,
        passed_probe_count=3,
        completion_sha256="e" * 64,
        completed_at=NOW,
    )
    session.add(completion)
    session.flush()
    return worker, completion


def test_0022_readiness_inventory_change_under_same_ids_revalidates_stale(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, bundle, closure = _validated_candidate(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        manifest = _approve(session, service, candidate_id, bundle, closure)
        original_ids = (
            candidate.candidate_id,
            candidate.projection_epoch_id,
        )

        inventory, requirements = _seed_inventory(session, ids, candidate)
        revalidation = _revalidate(session, ids, service, candidate_id)

        assert original_ids == (
            candidate.candidate_id,
            inventory.projection_epoch_id,
        )
        assert len(requirements) == 3
        assert revalidation.result == "stale"
        assert (
            revalidation.observed_readiness_inventory_sha256
            != manifest.readiness_inventory_sha256
        )
        assert (
            revalidation.observed_readiness_completion_sha256
            == manifest.readiness_completion_sha256
        )


def test_0022_readiness_completion_change_under_same_ids_revalidates_stale(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, bundle, closure = _validated_candidate(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        inventory, requirements = _seed_inventory(session, ids, candidate)
        manifest = _approve(session, service, candidate_id, bundle, closure)
        original_ids = (
            candidate.candidate_id,
            candidate.projection_epoch_id,
            inventory.inventory_id,
        )

        worker, completion = _seed_readiness_completion(
            session, ids, candidate, inventory, requirements
        )
        revalidation = _revalidate(session, ids, service, candidate_id)

        assert original_ids == (
            candidate.candidate_id,
            worker.projection_epoch_id,
            completion.inventory_id,
        )
        assert revalidation.result == "stale"
        assert (
            revalidation.observed_readiness_inventory_sha256
            == manifest.readiness_inventory_sha256
        )
        assert (
            revalidation.observed_readiness_completion_sha256
            != manifest.readiness_completion_sha256
        )
