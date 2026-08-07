from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.candidate_manifest import bind_approval_manifest
from dish_pg.recovery_control import (
    RecoveredPhysicalState,
    RestoreControl,
    RestoreControlError,
    migration_revision_sha256,
    promote_restored_generation,
)
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.release_evidence import sha256_json
from dish_pg.transition import ProjectionService
from dish_pg.workflow import (
    MutationAdmissionClosed,
    RequestSpec,
    StaleAuthorityError,
    WorkflowAuthorityService,
)
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next, core_db
from tests.support.postgresql.workflow import (
    _admit,
    _execution,
    _register_run,
    workflow_db,
)

from tests.support.postgresql.recovery_control import (
    _control,
    _physical_state,
    _setup,
)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_name", "service_profile"),
        ("system_identifier", "wrong"),
        ("schema_head", "0028_old"),
        ("backup_manifest_sha256", "0" * 64),
        ("backup_evidence_sha256", "1" * 64),
        ("recovery_timeline_id", 4),
        ("recovery_target_type", "backup_end"),
        ("recovery_target_lsn", "0/0"),
        ("recovery_completion_lsn", "0/1"),
        ("recovery_target_instance_sha256", "2" * 64),
    ],
)
def test_restore_promotion_rejects_any_physical_evidence_mismatch(core_db, field, value):
    factory, ids = core_db
    with factory() as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        session.get(models.AuthorityGeneration, context["generation_id"]).schema_head = ALEMBIC_HEAD
        state = _physical_state()
        control = _control(context, ids, state)
        with pytest.raises(RestoreControlError, match="physical recovery evidence mismatch"):
            promote_restored_generation(
                session,
                control,
                recovered_state=replace(state, **{field: value}),
                clock=lambda: NOW + timedelta(minutes=2),
            )

def test_restore_promotion_rejects_aggregate_recovery_digest_mismatch(core_db):
    factory, ids = core_db
    with factory() as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        session.get(models.AuthorityGeneration, context["generation_id"]).schema_head = ALEMBIC_HEAD
        state = _physical_state()
        control = replace(_control(context, ids, state), recovery_evidence_sha256="f" * 64)
        with pytest.raises(RestoreControlError, match="recovery evidence digest mismatch"):
            promote_restored_generation(
                session, control, recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )

@pytest.mark.parametrize(
    "status",
    ["assembling", "aborted"],
    ids=["assembling", "aborted-rejected-or-superseded-product-state"],
)
def test_unauthorized_release_candidate_state_cannot_promote(core_db, status):
    factory, ids = core_db
    with factory() as session:
        context, _, _ = _setup(session, ids, candidate_status=status)
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="not authorized"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )

def test_ambiguous_duplicate_release_candidates_cannot_promote(core_db):
    factory, ids = core_db
    with factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            pytest.skip(
                "PostgreSQL candidate guards prevent constructing duplicate live authority"
            )
        context, epoch, first = _setup(session, ids, candidate_status="activated")
        duplicate = release_models.ReleaseCandidate(
            candidate_id=_next(ids),
            generation_id=context["generation_id"],
            source_import_batch_id=first.source_import_batch_id,
            shadow_baseline_id=first.shadow_baseline_id,
            projection_epoch_id=epoch.projection_epoch_id,
            source_release=first.source_release,
            source_commit="duplicate-activated",
            ledger_through_commit=first.ledger_through_commit,
            schema_head=first.schema_head,
            dish_release=first.dish_release,
            honest_release=first.honest_release,
            protocol_release=first.protocol_release,
            openapi_release=first.openapi_release,
            routing_release=first.routing_release,
            status="assembling",
            candidate_revision=1,
            validation_bundle_sha256=None,
            created_at=NOW,
            validated_at=None,
            approved_at=None,
            terminal_at=None,
        )
        session.add(duplicate)
        session.flush()
        duplicate.status = "validated"
        duplicate.candidate_revision = 2
        duplicate.validation_bundle_sha256 = "d" * 64
        duplicate.validated_at = NOW
        session.flush()
        duplicate.status = "approved"
        duplicate.candidate_revision = 3
        duplicate.approved_at = NOW
        session.flush()
        duplicate.status = "activated"
        duplicate.candidate_revision = 4
        duplicate.terminal_at = NOW
        session.flush()
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="exactly one candidate"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )

def _assert_promotion_state(session, context, control, result) -> None:
    assert result.generation_id == control.generation_id
    assert session.get(models.AuthorityGeneration, context["generation_id"]).status == "retired"
    assert session.get(models.AuthorityGeneration, control.generation_id).status == "active"
    old_epoch = session.scalar(
        select(projection_models.ProjectionEpoch).where(
            projection_models.ProjectionEpoch.generation_id == context["generation_id"]
        )
    )
    assert old_epoch.status == "retired"
    assert old_epoch.external_effects_enabled is False
    new_epoch = session.get(projection_models.ProjectionEpoch, result.projection_epoch_id)
    assert new_epoch.status == "active"
    assert new_epoch.external_effects_enabled is False
    migration = session.get(models.AppliedMigrationEvent, result.migration_event_id)
    assert migration.migration_code_sha256 == migration_revision_sha256(ALEMBIC_HEAD)
    assert migration.details["recovery_evidence_sha256"] == control.recovery_evidence_sha256

def _assert_bootstrap_fencing(session, ids, context, control) -> None:
    service = WorkflowAuthorityService(session)
    with pytest.raises(StaleAuthorityError, match="generation is not active"):
        service.register_run(
            run_id=_next(ids), generation_id=context["generation_id"],
            owner_id="pre-restore", agent="service",
            capability_digest=hashlib.sha256(b"stale").digest(), registered_at=NOW,
        )
    with pytest.raises(StaleAuthorityError, match="requires external bootstrap"):
        service.register_run(
            run_id=_next(ids), generation_id=control.generation_id,
            owner_id="self-register", agent="service",
            capability_digest=hashlib.sha256(b"stale").digest(), registered_at=NOW,
        )
    run = service.register_run(
        run_id=_next(ids), generation_id=control.generation_id,
        owner_id="post-restore", agent="service",
        capability_digest=control.bootstrap_capability_digest,
        bootstrap_id=control.bootstrap_id, registered_at=NOW + timedelta(minutes=3),
    )
    with pytest.raises(MutationAdmissionClosed, match="deliberate reissue control"):
        service.admit_request(
            RequestSpec(
                request_id=_next(ids), generation_id=control.generation_id,
                run_id=run.run_id, owner_id=run.owner_id, principal_class="service",
                command_name="post_restore_mutation", canonical_payload={"allowed": False},
                protocol_release="protocol-1", dish_release="dish-42619b9",
                admitted_at=NOW + timedelta(minutes=3),
            )
        )
    with pytest.raises(StaleAuthorityError, match="already consumed"):
        service.register_run(
            run_id=_next(ids), generation_id=control.generation_id,
            owner_id="replay", agent="service",
            capability_digest=control.bootstrap_capability_digest,
            bootstrap_id=control.bootstrap_id, registered_at=NOW + timedelta(minutes=4),
        )

def test_corrupt_release_approval_digest_cannot_promote(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids)
        approval = session.scalar(
            select(release_models.CutoverApproval).where(
                release_models.CutoverApproval.candidate_id == candidate.candidate_id
            )
        )
        approval.approval_sha256 = "0" * 64
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="approval digest is corrupt"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )

def test_exact_approved_candidate_promotes_and_bootstraps_once(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, _ = _setup(session, ids)
        state = _physical_state()
        control = _control(context, ids, state)
        result = promote_restored_generation(
            session,
            control,
            recovered_state=state,
            clock=lambda: NOW + timedelta(minutes=2),
            uuid_factory=lambda: _next(ids),
        )
        _assert_promotion_state(session, context, control, result)
        _assert_bootstrap_fencing(session, ids, context, control)

def test_wrong_release_and_future_control_fail_before_promotion(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, _ = _setup(session, ids)
        state = _physical_state()
        control = _control(context, ids, state)
        with pytest.raises(RestoreControlError, match="issued in the future"):
            promote_restored_generation(session, control, recovered_state=state, clock=lambda: NOW)
        with pytest.raises(RestoreControlError, match="exactly one candidate"):
            promote_restored_generation(
                session,
                replace(control, protocol_release="wrong-protocol"),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )

def test_retired_generation_lease_cannot_be_renewed(workflow_db):
    factory, ids, context, task_id = workflow_db
    run_id, request_id, execution_id, operation_id, lease_id = [_next(ids) for _ in range(5)]
    with factory() as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        _admit(service, request_id=request_id, generation_id=context["generation_id"], run_id=run_id)
        _execution(
            service, execution_id=execution_id, request_id=request_id,
            generation_id=context["generation_id"], task_id=task_id,
            binding_id=context["binding_id"],
        )
        service.repo.capture_task_fence(
            execution_id=execution_id, generation_id=context["generation_id"],
            task_id=task_id, at=NOW,
        )
        service.create_operation(
            operation_id=operation_id, execution_id=execution_id, task_id=task_id,
            kind="initial", phase="prepare_required", persisted_actions=["prepare"],
            created_at=NOW,
        )
        service.acquire_actor_lease(
            lease_id=lease_id, execution_id=execution_id, operation_id=operation_id,
            run_id=run_id, owner_id="owner-1", actor_role="researcher",
            actor_attempt_sequence=1, issued_at=NOW, expires_at=NOW + timedelta(hours=1),
        )
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        generation.status, generation.retired_at = "retired", NOW + timedelta(minutes=1)
        session.flush()
        with pytest.raises(StaleAuthorityError, match="generation is not active"):
            service.renew_lease(
                lease_id=lease_id, execution_id=execution_id, run_id=run_id,
                owner_id="owner-1", now=NOW + timedelta(minutes=2),
                new_expiry=NOW + timedelta(hours=2),
            )
