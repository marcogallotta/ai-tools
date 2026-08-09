"""Shared recovery-control scenario builders for focused PostgreSQL tests."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import models
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.candidate_manifest import bind_approval_manifest
from dish_pg.recovery_control import RecoveredPhysicalState, RestoreControl
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.release_evidence import sha256_json
from dish_pg.transition import ProjectionService
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next


def _physical_state(**overrides) -> RecoveredPhysicalState:
    values = {
        "database_name": "dish_section2_restore_1",
        "system_identifier": "7600000000000000000",
        "schema_head": ALEMBIC_HEAD,
        "backup_manifest_sha256": "a" * 64,
        "backup_evidence_sha256": "b" * 64,
        "recovery_timeline_id": 3,
        "recovery_target_type": "lsn",
        "recovery_target_lsn": "0/ABCDEF0",
        "recovery_completion_lsn": "0/ABCDEF0",
        "recovery_target_instance_sha256": "c" * 64,
    }
    values.update(overrides)
    return RecoveredPhysicalState(**values)

def _control(context, ids, state: RecoveredPhysicalState) -> RestoreControl:
    return RestoreControl(
        external_control_id="section2-control-001",
        predecessor_generation_id=context["generation_id"],
        generation_id=_next(ids),
        bootstrap_id=_next(ids),
        bootstrap_capability_digest=hashlib.sha256(b"section2-current-actor").digest(),
        expected_database_name=state.database_name,
        expected_system_identifier=state.system_identifier,
        schema_head=state.schema_head,
        dish_release="dish-42619b9",
        honest_release="honest-1",
        protocol_release="protocol-1",
        openapi_release="openapi-1",
        routing_release="routing-1",
        backup_manifest_sha256=state.backup_manifest_sha256,
        backup_evidence_sha256=state.backup_evidence_sha256,
        recovery_timeline_id=state.recovery_timeline_id,
        recovery_target_type=state.recovery_target_type,
        recovery_target_lsn=state.recovery_target_lsn,
        recovery_completion_lsn=state.recovery_completion_lsn,
        recovery_target_instance_sha256=state.recovery_target_instance_sha256,
        recovery_evidence_sha256=state.evidence_sha256,
        issued_at=NOW + timedelta(minutes=1),
    )

def _candidate(session, ids, context, epoch_id, *, status: str):
    batch_id, baseline_id, candidate_id = _next(ids), _next(ids), _next(ids)
    session.add_all(
        [
            projection_models.SourceImportBatch(
                import_batch_id=batch_id,
                generation_id=context["generation_id"],
                import_run_id=context["import_run_id"],
                source_release="dish-42619b9",
                source_commit=str(candidate_id),
                source_database_sha256="a" * 64,
                source_sidecars={"fixture": "recovery-control"},
                ledger_through_commit="42619b9",
                expected_entities=1,
                imported_entities=1,
                status="complete",
                started_at=NOW,
                completed_at=NOW,
            ),
            projection_models.ShadowBaseline(
                shadow_baseline_id=baseline_id,
                generation_id=context["generation_id"],
                source_generation_identity=str(candidate_id),
                source_commit=str(candidate_id),
                baseline_sequence=(candidate_id.int % 1000000) + 1,
                status="closed",
                disqualification_reason=None,
                created_at=NOW,
                terminal_at=NOW,
            ),
        ]
    )
    candidate = release_models.ReleaseCandidate(
        candidate_id=candidate_id,
        generation_id=context["generation_id"],
        source_import_batch_id=batch_id,
        shadow_baseline_id=baseline_id,
        projection_epoch_id=epoch_id,
        source_release="dish-42619b9",
        source_commit=str(candidate_id),
        ledger_through_commit="42619b9",
        schema_head=ALEMBIC_HEAD,
        dish_release="dish-42619b9",
        honest_release="honest-1",
        protocol_release="protocol-1",
        openapi_release="openapi-1",
        routing_release="routing-1",
        status="assembling",
        candidate_revision=1,
        validation_bundle_sha256=None,
        created_at=NOW,
        validated_at=None,
        approved_at=None,
        terminal_at=None,
    )
    session.add(candidate)
    session.flush()
    if status == "aborted":
        candidate.status = "aborted"
        candidate.candidate_revision = 2
        candidate.terminal_at = NOW
        session.flush()
    elif status == "activated":
        candidate.status = "validated"
        candidate.candidate_revision = 2
        candidate.validation_bundle_sha256 = "c" * 64
        candidate.validated_at = NOW
        session.flush()
        candidate.status = "approved"
        candidate.candidate_revision = 3
        candidate.approved_at = NOW
        session.flush()
        candidate.status = "activated"
        candidate.candidate_revision = 4
        candidate.terminal_at = NOW
        session.flush()
    return candidate

def _approve_candidate(session, ids, candidate) -> None:
    active_registry = session.get(models.ActiveSectionRegistry, candidate.generation_id)
    reconciliation = projection_models.ProjectionReconciliationRun(
        reconciliation_run_id=_next(ids),
        generation_id=candidate.generation_id,
        projection_epoch_id=candidate.projection_epoch_id,
        corpus_identity=f"recovery-approval:{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        registry_version_id=active_registry.registry_version_id,
        observation_started_at=NOW,
        observation_completed_at=NOW,
        external_snapshot_identity="recovery-approval-snapshot",
        external_high_water=None,
        corpus_manifest_sha256="e" * 64,
        scope_complete=True,
        adapter_contract_version="asana-snapshot-v1",
        evidence_recorded_at=NOW,
        status="complete",
        expected_items=0,
        processed_items=0,
        started_at=NOW,
        completed_at=NOW,
    )
    session.add(reconciliation)
    session.flush()
    manifest = {"candidate_id": str(candidate.candidate_id), "authorized": True}
    digest = sha256_json(manifest)
    bundle = release_models.EvidenceBundle(
        bundle_id=_next(ids),
        candidate_id=candidate.candidate_id,
        bundle_kind="release_candidate",
        bundle_revision=1,
        manifest=manifest,
        manifest_sha256=digest,
        built_at=NOW,
    )
    candidate.status = "validated"
    candidate.candidate_revision = 2
    candidate.validation_bundle_sha256 = digest
    candidate.validated_at = NOW
    session.add(bundle)
    session.commit()
    approval_payload = {"candidate_id": str(candidate.candidate_id)}
    approval_body = {
        "candidate_id": str(candidate.candidate_id),
        "evidence_bundle_sha256": bundle.manifest_sha256,
        "approver": "section2-test",
        "statement": "Authorize exact validated candidate.",
        "payload": approval_payload,
        "approved_at": NOW.isoformat(),
    }
    approval = release_models.CutoverApproval(
        approval_id=_next(ids),
        candidate_id=candidate.candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver=approval_body["approver"],
        approval_statement=approval_body["statement"],
        approval_payload=approval_payload,
        approval_sha256=sha256_json(approval_body),
        approved_at=NOW,
    )
    session.add(approval)
    session.commit()
    bind_approval_manifest(
        session,
        uuid_factory=lambda: _next(ids),
        approval=approval,
        candidate=candidate,
        bound_at=NOW,
    )
    session.commit()
    candidate = session.get(release_models.ReleaseCandidate, candidate.candidate_id)
    candidate.status = "approved"
    candidate.candidate_revision = 3
    candidate.approved_at = NOW
    session.flush()


def _activate_candidate(
    session,
    ids,
    candidate,
    *,
    burned_at=NOW,
    legacy_bundle_id: str = "section2-activated-candidate",
) -> models.AuthorityActivation:
    approval = session.scalar(
        select(release_models.CutoverApproval).where(
            release_models.CutoverApproval.candidate_id == candidate.candidate_id
        )
    )
    manifest = session.scalar(
        select(manifest_models.ReleaseCandidateManifest).where(
            manifest_models.ReleaseCandidateManifest.candidate_id == candidate.candidate_id
        )
    )
    assert approval is not None and manifest is not None
    activation = models.AuthorityActivation(
        activation_id=_next(ids),
        generation_id=candidate.generation_id,
        import_run_id=manifest.source_import_run_id,
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
    candidate.status = "activated"
    candidate.candidate_revision += 1
    candidate.terminal_at = burned_at
    session.add(activation)
    session.commit()
    return activation

def _setup(session, ids, *, candidate_status="approved"):
    context = _bootstrap_registry(session, ids, generation_status="active")
    generation = session.get(models.AuthorityGeneration, context["generation_id"])
    generation.schema_head = ALEMBIC_HEAD
    epoch = ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
        generation_id=context["generation_id"],
        activation_reason="pre-restore live epoch",
        created_at=NOW,
        external_effects_enabled=True,
    )
    candidate = _candidate(
        session, ids, context, epoch.projection_epoch_id, status=candidate_status
    )
    session.commit()
    if candidate_status == "approved":
        candidate = session.get(release_models.ReleaseCandidate, candidate.candidate_id)
        _approve_candidate(session, ids, candidate)
        session.commit()
    return context, epoch, candidate
