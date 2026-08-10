"""Focused service/model coverage for candidate-state manifests."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.candidate_manifest import build_candidate_manifest, revalidate_candidate_manifest
from dish_pg.database import session_scope
from dish_pg.release_history import SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT
from dish_pg.services import (
    CoreAuthorityService,
    ImportedOperationHistorySpec,
    ImportedServiceLeaseSpec,
    ImportedVerificationCycleSpec,
    ImportedWorkflowOperationSpec,
)
from tests.support.postgresql.release import HASH_A, _prepare_candidate, _record_final_closure
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _add_supplemental_history(session, ids, context, task_id, *, minute: int = 3):
    primary = session.get(models.ImportRun, context["import_run_id"])
    assert primary is not None
    imported_at = NOW + timedelta(minutes=minute)
    completed_at = imported_at + timedelta(seconds=30)
    operation_id = _next(ids)
    cycle_id = _next(ids)
    lease_id = _next(ids)
    import_run_id = _next(ids)
    run = models.ImportRun(
        import_run_id=import_run_id,
        source_commit="9" * 40,
        source_release=f"dish@{'9' * 40}",
        legacy_generation_id=primary.legacy_generation_id,
        baseline_high_water_mark=f"terminal-history:test:{import_run_id}",
        source_bundle_sha256="b" * 64,
        status="complete",
        started_at=imported_at,
        completed_at=imported_at,
        provenance={
            "resolved_by": "candidate-manifest-test",
            "import_kind": "terminal-history-backfill-v1",
            "task_id": str(task_id),
            "legacy_task_gid": "123456789",
            "generation_id": str(context["generation_id"]),
            "primary_import_run_id": str(context["import_run_id"]),
            "source_path": "/tmp/candidate-manifest-test.ndjson",
            "source_format": "dish-terminal-history-backfill-source-v1",
            "source_record_count": 1,
            "source_bundle_hash_method": "sha256-file-bytes",
            "candidate_attestation": SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT,
        },
    )
    session.add(run)
    session.flush()
    CoreAuthorityService(session).backfill_imported_operation_history(
        generation_id=context["generation_id"],
        task_id=task_id,
        import_run_id=import_run_id,
        contract_binding_id=context["binding_id"],
        history=ImportedOperationHistorySpec(
            operations=(
                ImportedWorkflowOperationSpec(
                    operation_id=operation_id,
                    kind="planning",
                    status="completed",
                    phase="terminal",
                    terminal_outcome="planning_handoff_confirmed",
                    created_at=imported_at,
                    completed_at=completed_at,
                ),
            ),
            verification_cycles=(
                ImportedVerificationCycleSpec(
                    cycle_id=cycle_id,
                    operation_id=operation_id,
                    cycle_sequence=1,
                    outcome="approved",
                    created_at=imported_at,
                    completed_at=completed_at,
                ),
            ),
            leases=(
                ImportedServiceLeaseSpec(
                    lease_id=lease_id,
                    operation_id=operation_id,
                    source_run_id=f"legacy-run-{minute}",
                    owner_id="owner-1",
                    lease_kind="actor",
                    actor_attempt_sequence=minute + 1,
                    verification_cycle_id=cycle_id,
                    issued_at=imported_at,
                    expires_at=imported_at + timedelta(minutes=5),
                    released_at=completed_at,
                ),
            ),
        ),
    )
    return run


def _approve_candidate(session, ids, context, task_id, *, supplemental: bool = False):
    service, candidate_id = _prepare_candidate(session, ids, context, task_id)
    if supplemental:
        _add_supplemental_history(session, ids, context, task_id, minute=0)
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
    assert manifest.manifest_version == 3
    assert manifest.approval_reconciliation_run_id is not None
    assert manifest.readiness_inventory_sha256 is None
    assert manifest.readiness_completion_sha256 is None
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


def test_0022_supplemental_terminal_history_changes_candidate_manifest(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)

        nested = session.begin_nested()
        primary_only = build_candidate_manifest(
            session,
            uuid_factory=lambda: _next(ids),
            candidate=candidate,
            built_at=NOW + timedelta(minutes=1),
        )
        primary_fingerprint = primary_only.canonical_fingerprint
        primary_import_digest = primary_only.import_completion_sha256
        primary_builder = primary_only.builder_contract_version
        nested.rollback()
        session.expire_all()

        _add_supplemental_history(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)
        with_supplemental = build_candidate_manifest(
            session,
            uuid_factory=lambda: _next(ids),
            candidate=candidate,
            built_at=NOW + timedelta(minutes=4),
        )

        assert primary_builder == "candidate-authority-v3"
        assert with_supplemental.builder_contract_version == (
            SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT
        )
        assert with_supplemental.canonical_fingerprint != primary_fingerprint
        assert with_supplemental.import_completion_sha256 != primary_import_digest


def test_0022_revalidation_catches_supplemental_terminal_history_change(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, manifest = _approve_candidate(
            session, ids, context, task_id
        )
        assert manifest.builder_contract_version == "candidate-authority-v3"

        _add_supplemental_history(session, ids, context, task_id)
        revalidation = _revalidate(
            session, ids, service, candidate_id, minute=5
        )

        assert revalidation.result == "stale"
        assert revalidation.observed_fingerprint != manifest.canonical_fingerprint
        assert (
            revalidation.observed_import_completion_sha256
            != manifest.import_completion_sha256
        )


@pytest.mark.parametrize("entity_kind", ("operation", "cycle", "lease"))
def test_0022_revalidation_catches_mutated_supplemental_history_row(
    workflow_db, entity_kind: str
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, manifest = _approve_candidate(
            session, ids, context, task_id, supplemental=True
        )
        assert manifest.builder_contract_version == (
            SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT
        )

        model = {
            "operation": wf.WorkflowOperation,
            "cycle": wf.VerificationCycle,
            "lease": wf.ServiceLease,
        }[entity_kind]
        row = session.scalar(
            select(model).where(
                model.import_run_id.is_not(None),
                model.import_run_id != context["import_run_id"],
            )
        )
        assert row is not None
        if entity_kind == "operation":
            row.phase = "terminal-history-mutated"
        elif entity_kind == "cycle":
            row.outcome = "history-mutated"
        else:
            row.owner_id = "owner-mutated"
        session.flush()

        revalidation = _revalidate(
            session, ids, service, candidate_id, minute=5
        )
        assert revalidation.result == "stale"
        assert revalidation.observed_fingerprint != manifest.canonical_fingerprint
        assert (
            revalidation.observed_import_completion_sha256
            != manifest.import_completion_sha256
        )
