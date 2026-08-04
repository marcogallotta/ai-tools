"""Focused service/model coverage for candidate-state manifests."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.candidate_manifest import revalidate_candidate_manifest
from dish_pg.database import session_scope
from tests.support.postgresql.release import HASH_A, _prepare_candidate, _record_final_closure
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _approve_candidate(session, ids, context, task_id):
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
        closed_through_at=NOW + timedelta(minutes=2),
    )
    approval = service.approve_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver="Marco",
        approval_statement="Approve exact authority manifest.",
        approval_payload={
            "final_asana_closure_id": str(closure.closure_id),
            "final_asana_closure_sha256": closure.closure_sha256,
        },
        approved_at=closure.recorded_at,
    )
    manifest = session.scalar(
        select(manifest_models.ReleaseCandidateManifest).where(
            manifest_models.ReleaseCandidateManifest.candidate_id == candidate_id
        )
    )
    binding = session.scalar(
        select(manifest_models.CutoverApprovalManifestBinding).where(
            manifest_models.CutoverApprovalManifestBinding.approval_id
            == approval.approval_id
        )
    )
    assert manifest is not None and binding is not None
    assert binding.canonical_fingerprint == manifest.canonical_fingerprint
    assert manifest.manifest_version == 2
    return service, candidate_id, manifest


def _revalidate(session, ids, service, candidate_id, *, minute: int):
    return revalidate_candidate_manifest(
        session,
        uuid_factory=lambda: _next(ids),
        candidate=service._candidate(candidate_id),
        revalidated_at=NOW + timedelta(minutes=minute),
    )


def test_0022_approval_binds_manifest_and_registry_change_revalidates_stale(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, _manifest = _approve_candidate(
            session, ids, context, task_id
        )

        current = session.get(models.ActiveSectionRegistry, context["generation_id"])
        assert current is not None
        replacement_id = _next(ids)
        session.add(
            models.SectionRegistryVersion(
                registry_version_id=replacement_id,
                generation_id=context["generation_id"],
                version_number=2,
                import_run_id=context["import_run_id"],
                contract_binding_id=context["binding_id"],
                registry_sha256=HASH_A,
                created_at=NOW + timedelta(minutes=3),
            )
        )
        session.flush()
        current.registry_version_id = replacement_id
        current.registry_revision += 1
        current.updated_at = NOW + timedelta(minutes=3)
        session.flush()

        revalidation = _revalidate(
            session, ids, service, candidate_id, minute=4
        )
        assert revalidation.result == "stale"


def test_0022_active_mapping_membership_change_revalidates_stale(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, manifest = _approve_candidate(
            session, ids, context, task_id
        )
        mapping = session.scalar(
            select(tx.TaskProjectionMapping).where(
                tx.TaskProjectionMapping.generation_id == context["generation_id"],
                tx.TaskProjectionMapping.projection_epoch_id
                == service._candidate(candidate_id).projection_epoch_id,
                tx.TaskProjectionMapping.state == "active",
            )
        )
        assert mapping is not None
        original_ids = (
            mapping.generation_id,
            mapping.projection_epoch_id,
            mapping.mapping_id,
        )
        mapping.state = "retired"
        mapping.mapping_revision += 1
        mapping.retired_at = NOW + timedelta(minutes=3)
        session.flush()

        revalidation = _revalidate(
            session, ids, service, candidate_id, minute=4
        )
        assert original_ids == (
            mapping.generation_id,
            mapping.projection_epoch_id,
            mapping.mapping_id,
        )
        assert revalidation.result == "stale"
        assert (
            revalidation.observed_mapping_membership_sha256
            != manifest.mapping_membership_sha256
        )


def test_0022_import_completion_change_revalidates_stale(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, manifest = _approve_candidate(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        batch = session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
        assert batch is not None
        original_ids = (batch.import_batch_id, batch.import_run_id, batch.generation_id)
        batch.status = "failed"
        session.flush()

        revalidation = _revalidate(
            session, ids, service, candidate_id, minute=4
        )
        assert original_ids == (
            batch.import_batch_id,
            batch.import_run_id,
            batch.generation_id,
        )
        assert revalidation.result == "stale"
        assert (
            revalidation.observed_import_completion_sha256
            != manifest.import_completion_sha256
        )


def test_0022_reconciliation_evidence_change_revalidates_stale(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, manifest = _approve_candidate(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        reconciliation = session.scalar(
            select(tx.ProjectionReconciliationRun).where(
                tx.ProjectionReconciliationRun.generation_id
                == candidate.generation_id,
                tx.ProjectionReconciliationRun.projection_epoch_id
                == candidate.projection_epoch_id,
            )
        )
        assert reconciliation is not None
        original_ids = (
            reconciliation.reconciliation_run_id,
            reconciliation.generation_id,
            reconciliation.projection_epoch_id,
        )
        reconciliation.status = "blocked"
        session.flush()

        revalidation = _revalidate(
            session, ids, service, candidate_id, minute=4
        )
        assert original_ids == (
            reconciliation.reconciliation_run_id,
            reconciliation.generation_id,
            reconciliation.projection_epoch_id,
        )
        assert revalidation.result == "stale"
        assert (
            revalidation.observed_reconciliation_evidence_sha256
            != manifest.reconciliation_evidence_sha256
        )
