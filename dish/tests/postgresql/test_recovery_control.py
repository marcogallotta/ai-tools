from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.candidate_manifest import bind_approval_manifest
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.recovery_control import (
    RecoveredPhysicalState,
    _authorized_release_candidate,
    RestoreControl,
    RestoreControlError,
    authorize_recovery_qualification,
    migration_revision_sha256,
    promote_restored_generation,
    record_recovery_readiness,
    rehydrate_restored_generation,
)
from dish_pg.recovery_rehydration import RecoveryQualificationSpec
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.read_model import PostgresReadModel, ReadModelError
from dish_pg.transition import ProjectionService
from dish_pg.workflow import (
    MutationAdmissionClosed,
    RequestSpec,
    StaleAuthorityError,
    WorkflowAuthorityService,
)
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next, core_db
from tests.support.postgresql.workflow import (
    NOW as WORKFLOW_NOW,
    _admit,
    _execution,
    _register_run,
    workflow_db,
)

from tests.support.postgresql.recovery_control import (
    _control,
    _physical_state,
    _setup_synthetic_recovery_state,
    recovery_db,
)

from tests.support.postgresql.release import (
    _complete_active_mapping_reconciliation,
    _record_runtime_and_worker_readiness_report,
)
from tests.support.postgresql.stage8_cutover_evidence_gates import (
    _burn_rollback,
    _prepare_fenced_recertified_cutover,
)


def _control_for_candidate(context, ids, state, candidate):
    return replace(
        _control(context, ids, state),
        schema_head=candidate.schema_head,
        dish_release=candidate.dish_release,
        honest_release=candidate.honest_release,
        protocol_release=candidate.protocol_release,
        openapi_release=candidate.openapi_release,
        routing_release=candidate.routing_release,
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
        context, _, _ = _setup_synthetic_recovery_state(session, ids, candidate_status=status)
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="not rollback-burned"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )

def test_synthetic_ambiguous_duplicate_release_candidates_cannot_promote(core_db):
    factory, ids = core_db
    with factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            pytest.skip(
                "PostgreSQL candidate guards prevent constructing duplicate live authority"
            )
        context, epoch, first = _setup_synthetic_recovery_state(session, ids, candidate_status="activated")
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

def _assert_bootstrap_fencing(session, ids, context, control, *, now=NOW) -> None:
    service = WorkflowAuthorityService(session)
    with pytest.raises(StaleAuthorityError, match="generation is not active"):
        service.register_run(
            run_id=_next(ids), generation_id=context["generation_id"],
            owner_id="pre-restore", agent="service",
            capability_digest=hashlib.sha256(b"stale").digest(), registered_at=now,
        )
    with pytest.raises(StaleAuthorityError, match="requires external bootstrap"):
        service.register_run(
            run_id=_next(ids), generation_id=control.generation_id,
            owner_id="self-register", agent="service",
            capability_digest=hashlib.sha256(b"stale").digest(), registered_at=now,
        )
    run = service.register_run(
        run_id=_next(ids), generation_id=control.generation_id,
        owner_id="post-restore", agent="service",
        capability_digest=control.bootstrap_capability_digest,
        bootstrap_id=control.bootstrap_id, registered_at=now + timedelta(minutes=3),
    )
    with pytest.raises(MutationAdmissionClosed, match="deliberate reissue control"):
        service.admit_request(
            RequestSpec(
                request_id=_next(ids), generation_id=control.generation_id,
                run_id=run.run_id, owner_id=run.owner_id, principal_class="service",
                command_name="post_restore_mutation", canonical_payload={"allowed": False},
                protocol_release="protocol-1", dish_release="dish-42619b9",
                admitted_at=now + timedelta(minutes=3),
            )
        )
    with pytest.raises(StaleAuthorityError, match="already consumed"):
        service.register_run(
            run_id=_next(ids), generation_id=control.generation_id,
            owner_id="replay", agent="service",
            capability_digest=control.bootstrap_capability_digest,
            bootstrap_id=control.bootstrap_id, registered_at=now + timedelta(minutes=4),
        )

def test_synthetic_corrupt_release_approval_digest_cannot_promote(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        dish_release = generation.dish_release
        _service, candidate_id, _cutover_run_id = _burn_rollback(
            session, ids, context, task_id, dish_release=dish_release
        )

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        approval = session.scalar(
            select(release_models.CutoverApproval).where(
                release_models.CutoverApproval.candidate_id == candidate_id
            )
        )
        assert approval is not None
        approval.approval_sha256 = "0" * 64
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="approval digest is corrupt"):
            promote_restored_generation(
                session,
                _control_for_candidate(context, ids, state, candidate),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            )


def test_approved_but_unburned_candidate_cannot_promote(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        dish_release = generation.dish_release
        service, candidate_id, _closure, _run, _fence = (
            _prepare_fenced_recertified_cutover(
                session, ids, context, task_id, dish_release=dish_release
            )
        )
        assert service._candidate(candidate_id).status == "approved"

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        state = _physical_state()
        control = _control_for_candidate(context, ids, state, candidate)
        with pytest.raises(RestoreControlError, match="not rollback-burned: approved"):
            promote_restored_generation(
                session,
                control,
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
                uuid_factory=lambda: _next(ids),
            )



def test_legitimate_burned_candidate_promotes_and_bootstraps_once(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        dish_release = generation.dish_release
        service, candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id, dish_release=dish_release
        )
        candidate = service._candidate(candidate_id)
        assert candidate.status == "activated"
        assert session.get(release_models.CutoverRun, cutover_run_id).state == "rollback_burned"
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"]
            )
        )
        assert activation is not None
        assert activation.rollback_burned_at == candidate.terminal_at

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        state = _physical_state()
        control = _control_for_candidate(context, ids, state, candidate)
        result = promote_restored_generation(
            session,
            control,
            recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            uuid_factory=lambda: _next(ids),
        )
        _assert_promotion_state(session, context, control, result)
        _assert_bootstrap_fencing(
            session, ids, context, control, now=WORKFLOW_NOW + timedelta(minutes=8)
        )


def test_wrong_release_and_future_control_fail_before_promotion(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        dish_release = generation.dish_release
        service, candidate_id, _closure, _run, _fence = (
            _prepare_fenced_recertified_cutover(
                session, ids, context, task_id, dish_release=dish_release
            )
        )
        assert service._candidate(candidate_id).status == "approved"

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        state = _physical_state()
        control = _control_for_candidate(context, ids, state, candidate)
        with pytest.raises(RestoreControlError, match="issued in the future"):
            promote_restored_generation(session, control, recovered_state=state, clock=lambda: NOW)
        with pytest.raises(RestoreControlError, match="exactly one candidate"):
            promote_restored_generation(
                session,
                replace(control, protocol_release="wrong-protocol"),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
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


def test_recovery_remains_valid_after_legitimate_post_burn_readiness(recovery_db):
    factory, ids, context, task_id = recovery_db
    with factory() as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        dish_release = generation.dish_release
        service, candidate_id, _cutover_run_id = _burn_rollback(
            session, ids, context, task_id, dish_release=dish_release
        )
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            candidate_id=candidate_id,
            corpus_identity="cc5-recovery-post-burn",
            started_at=WORKFLOW_NOW + timedelta(minutes=6),
            completed_at=WORKFLOW_NOW + timedelta(minutes=6),
        )
        _record_runtime_and_worker_readiness_report(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=WORKFLOW_NOW + timedelta(minutes=6),
        )
        candidate = service._candidate(candidate_id)
        active = session.get(models.AuthorityGeneration, context["generation_id"])
        state = _physical_state()
        control = replace(
            _control(context, ids, state),
            schema_head=candidate.schema_head,
            dish_release=candidate.dish_release,
            honest_release=candidate.honest_release,
            protocol_release=candidate.protocol_release,
            openapi_release=candidate.openapi_release,
            routing_release=candidate.routing_release,
        )
        authorized = _authorized_release_candidate(
            session, active=active, control=control
        )
        assert authorized.candidate_id == candidate_id



def test_authorized_rehydration_restores_current_authority_and_keeps_transients_fenced(recovery_db):
    factory, ids, context, task_id = recovery_db
    pending_effect_id = None
    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        service, candidate_id, _ = _burn_rollback(
            session, ids, context, task_id, dish_release=generation.dish_release
        )
        candidate = service._candidate(candidate_id)
        pending_effect_id = ProjectionService(
            session, uuid_factory=lambda: _next(ids)
        )._record_event(
            generation_id=context["generation_id"],
            execution_id=None,
            task_id=task_id,
            event_type="reproject",
            payload={"reason": "recovery-regression"},
            source_route="service",
            origin="live",
            created_at=WORKFLOW_NOW + timedelta(minutes=7),
        ).projection_event_id
        predecessor_state = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        predecessor_membership_head = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        predecessor_identity = (
            predecessor_state.current_content_version_id,
            predecessor_state.dish_version,
            predecessor_membership_head.membership_revision,
            predecessor_state.placement_version,
            predecessor_state.completion_version,
        )

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        state = _physical_state()
        control = _control_for_candidate(context, ids, state, candidate)
        promote_restored_generation(
            session,
            control,
            recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            uuid_factory=lambda: _next(ids),
        )
        with pytest.raises(ReadModelError, match="incomplete scalar/membership authority"):
            PostgresReadModel(session, cursor_secret=b"r" * 32).task_view(task_id)
        predecessor_state_after_promotion = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        predecessor_membership_head = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        predecessor_content = session.get(
            models.ContentVersion, predecessor_state_after_promotion.current_content_version_id
        )
        predecessor_membership = session.scalar(
            select(models.CurrentTaskProjectMembership).where(
                models.CurrentTaskProjectMembership.generation_id == context["generation_id"],
                models.CurrentTaskProjectMembership.task_id == task_id,
            )
        )
        predecessor_pending_effect = session.get(
            projection_models.ProjectionOutboxEvent, pending_effect_id
        )
        predecessor_forensic_snapshot = (
            predecessor_state_after_promotion.current_content_version_id,
            predecessor_state_after_promotion.dish_version,
            predecessor_membership_head.membership_revision,
            predecessor_state_after_promotion.placement_version,
            predecessor_state_after_promotion.completion_version,
            predecessor_content.content_identity,
            predecessor_content.title,
            predecessor_content.body,
            predecessor_membership.latest_event_id,
            predecessor_membership.is_member,
            predecessor_membership.membership_revision,
            predecessor_state_after_promotion.section_id,
            predecessor_state_after_promotion.registry_version_id,
            predecessor_state_after_promotion.completed,
            predecessor_pending_effect.projection_event_id,
            predecessor_pending_effect.state,
            predecessor_pending_effect.intent_payload,
        )
        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        current_run = workflow.register_run(
            run_id=_next(ids),
            generation_id=control.generation_id,
            owner_id="post-restore",
            agent="service",
            capability_digest=control.bootstrap_capability_digest,
            bootstrap_id=control.bootstrap_id,
            registered_at=WORKFLOW_NOW + timedelta(minutes=9),
        )
        with pytest.raises(MutationAdmissionClosed, match="deliberate reissue control"):
            workflow.admit_request(
                RequestSpec(
                    request_id=_next(ids),
                    generation_id=control.generation_id,
                    run_id=current_run.run_id,
                    owner_id=current_run.owner_id,
                    principal_class="service",
                    command_name="recovery_probe",
                    canonical_payload={"phase": "before-rehydration"},
                    protocol_release=control.protocol_release,
                    dish_release=control.dish_release,
                    admitted_at=WORKFLOW_NOW + timedelta(minutes=9),
                )
            )

        result = rehydrate_restored_generation(
            session,
            control,
            recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=10),
        )
        replay = rehydrate_restored_generation(
            session,
            control,
            recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=11),
        )
        assert replay.replayed is True
        assert replay.repair_event_id == result.repair_event_id
        assert replay.import_run_id == result.import_run_id
        assert result.task_count == 1

        view = PostgresReadModel(session, cursor_secret=b"r" * 32).task_view(task_id)
        assert view.title == "[ready] Exact imported task"
        assert view.body == "Canonical body\n---\nStatus: ready\n"
        assert view.section_id == context["section_id"]
        assert view.completed is False
        successor_state = session.get(
            models.DishState, (control.generation_id, task_id)
        )
        successor_membership_head = session.get(
            models.TaskMembershipHead, (control.generation_id, task_id)
        )
        assert successor_state is not None and successor_membership_head is not None
        assert (
            successor_state.dish_version,
            successor_membership_head.membership_revision,
            successor_state.placement_version,
            successor_state.completion_version,
        ) == predecessor_identity[1:]

        predecessor_state = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        predecessor_membership_head = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        assert (
            predecessor_state.current_content_version_id,
            predecessor_state.dish_version,
            predecessor_membership_head.membership_revision,
            predecessor_state.placement_version,
            predecessor_state.completion_version,
        ) == predecessor_identity
        predecessor_content = session.get(
            models.ContentVersion, predecessor_state.current_content_version_id
        )
        predecessor_membership = session.scalar(
            select(models.CurrentTaskProjectMembership).where(
                models.CurrentTaskProjectMembership.generation_id == context["generation_id"],
                models.CurrentTaskProjectMembership.task_id == task_id,
            )
        )
        predecessor_pending_effect = session.get(
            projection_models.ProjectionOutboxEvent, pending_effect_id
        )
        assert (
            predecessor_state.current_content_version_id,
            predecessor_state.dish_version,
            predecessor_membership_head.membership_revision,
            predecessor_state.placement_version,
            predecessor_state.completion_version,
            predecessor_content.content_identity,
            predecessor_content.title,
            predecessor_content.body,
            predecessor_membership.latest_event_id,
            predecessor_membership.is_member,
            predecessor_membership.membership_revision,
            predecessor_state.section_id,
            predecessor_state.registry_version_id,
            predecessor_state.completed,
            predecessor_pending_effect.projection_event_id,
            predecessor_pending_effect.state,
            predecessor_pending_effect.intent_payload,
        ) == predecessor_forensic_snapshot
        repair = session.get(models.AppliedMigrationEvent, result.repair_event_id)
        unresolved = repair.details["transient_state"]["unresolved_external_effects"]
        assert str(pending_effect_id) in {row["projection_event_id"] for row in unresolved}
        assert repair.details["external_effects_enabled"] is False
        assert session.scalar(
            select(projection_models.ProjectionOutboxEvent).where(
                projection_models.ProjectionOutboxEvent.generation_id == control.generation_id
            )
        ) is None
        epoch = session.scalar(
            select(projection_models.ProjectionEpoch).where(
                projection_models.ProjectionEpoch.generation_id == control.generation_id,
                projection_models.ProjectionEpoch.status == "active",
            )
        )
        assert epoch.external_effects_enabled is False
        assert ProjectionService(session).claim_next(
            worker_id="recovery-regression-worker",
            now=WORKFLOW_NOW + timedelta(minutes=10),
            ttl=timedelta(minutes=1),
        ) is None

        pre_write_completion_version = successor_state.completion_version
        port = PostgresCommandPort(
            session, cursor_secret=b"r" * 32, uuid_factory=lambda: _next(ids)
        )
        read_result = port.execute(
            CommandCall(
                command_name="read",
                arguments={"task_id": str(task_id)},
                owner_id=current_run.owner_id,
                principal_class="admin",
                run_id=current_run.run_id,
                request_id=None,
                now=WORKFLOW_NOW + timedelta(minutes=10),
                protocol_release=control.protocol_release,
            )
        )
        assert read_result.ok is True
        assert read_result.data["title"] == "[ready] Exact imported task"

        def unrelated_request(at, phase):
            return RequestSpec(
                request_id=_next(ids),
                generation_id=control.generation_id,
                run_id=current_run.run_id,
                owner_id=current_run.owner_id,
                principal_class="admin",
                command_name="recovery_probe",
                canonical_payload={
                    "command": "recovery_probe",
                    "arguments": {"phase": phase},
                    "owner_id": current_run.owner_id,
                    "run_id": str(current_run.run_id),
                },
                protocol_release=control.protocol_release,
                dish_release=control.dish_release,
                admitted_at=at,
            )

        with pytest.raises(MutationAdmissionClosed, match="pending recovery readiness"):
            workflow.admit_request(
                unrelated_request(WORKFLOW_NOW + timedelta(minutes=10), "rehydrated-only")
            )

        qualification_request_id = _next(ids)
        qualification_arguments = {
            "task_id": str(task_id),
            "reason": "post-restore recovery qualification",
        }
        qualification_payload = {
            "command": "reopen-planning",
            "arguments": qualification_arguments,
            "owner_id": current_run.owner_id,
            "run_id": str(current_run.run_id),
        }
        qualification = authorize_recovery_qualification(
            session,
            control,
            recovered_state=state,
            spec=RecoveryQualificationSpec(
                request_id=qualification_request_id,
                run_id=current_run.run_id,
                owner_id=current_run.owner_id,
                principal_class="admin",
                command_name="reopen-planning",
                canonical_payload=qualification_payload,
            ),
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=11),
        )
        qualification_replay = authorize_recovery_qualification(
            session,
            control,
            recovered_state=state,
            spec=RecoveryQualificationSpec(
                request_id=qualification_request_id,
                run_id=current_run.run_id,
                owner_id=current_run.owner_id,
                principal_class="admin",
                command_name="reopen-planning",
                canonical_payload=qualification_payload,
            ),
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=12),
        )
        assert qualification_replay.replayed is True
        assert qualification_replay.qualification_event_id == qualification.qualification_event_id

        with pytest.raises(MutationAdmissionClosed, match="pending recovery readiness"):
            workflow.admit_request(
                unrelated_request(WORKFLOW_NOW + timedelta(minutes=11), "qualification-reserved")
            )
        with pytest.raises(RestoreControlError, match="lacks exact committed qualification"):
            record_recovery_readiness(
                session,
                control,
                recovered_state=state,
                qualification_request_id=qualification_request_id,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=12),
            )

        write_result = port.execute(
            CommandCall(
                command_name="reopen-planning",
                arguments=qualification_arguments,
                owner_id=current_run.owner_id,
                principal_class="admin",
                run_id=current_run.run_id,
                request_id=qualification_request_id,
                now=WORKFLOW_NOW + timedelta(minutes=12),
                protocol_release=control.protocol_release,
            )
        )
        assert write_result.ok is True, (write_result.code, write_result.data)
        updated = PostgresReadModel(session, cursor_secret=b"r" * 32).task_view(task_id)
        assert updated.completed is False
        assert session.get(
            models.DishState, (control.generation_id, task_id)
        ).completion_version == pre_write_completion_version + 1
        obligation = session.scalar(
            select(wf.InvocationAuditObligation).where(
                wf.InvocationAuditObligation.request_id == qualification_request_id
            )
        )
        assert obligation is not None
        workflow.repair_invocation_audit(
            obligation_id=obligation.obligation_id,
            repair_identity=f"recovery-qualification:{qualification_request_id}",
            source="postgresql",
            payload={
                "request_id": str(qualification_request_id),
                "qualification_event_id": str(qualification.qualification_event_id),
            },
            outcome="fulfilled",
            recorded_at=WORKFLOW_NOW + timedelta(minutes=13),
        )

        with pytest.raises(MutationAdmissionClosed, match="pending recovery readiness"):
            workflow.admit_request(
                unrelated_request(WORKFLOW_NOW + timedelta(minutes=13), "proof-complete-not-ready")
            )

        readiness = record_recovery_readiness(
            session,
            control,
            recovered_state=state,
            qualification_request_id=qualification_request_id,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=14),
        )
        readiness_replay = record_recovery_readiness(
            session,
            control,
            recovered_state=state,
            qualification_request_id=qualification_request_id,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=15),
        )
        assert readiness_replay.replayed is True
        assert readiness_replay.readiness_event_id == readiness.readiness_event_id
        post_readiness_qualification_replay = authorize_recovery_qualification(
            session,
            control,
            recovered_state=state,
            spec=RecoveryQualificationSpec(
                request_id=qualification_request_id,
                run_id=current_run.run_id,
                owner_id=current_run.owner_id,
                principal_class="admin",
                command_name="reopen-planning",
                canonical_payload=qualification_payload,
            ),
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=15),
        )
        assert post_readiness_qualification_replay.replayed is True
        assert (
            post_readiness_qualification_replay.qualification_event_id
            == qualification.qualification_event_id
        )
        ordinary = workflow.admit_request(
            unrelated_request(WORKFLOW_NOW + timedelta(minutes=15), "readiness-open")
        )
        assert ordinary.replayed is False
        assert ordinary.outcome is None

        successor_event = session.scalar(
            select(projection_models.ProjectionOutboxEvent).where(
                projection_models.ProjectionOutboxEvent.generation_id == control.generation_id
            )
        )
        assert successor_event is not None
        assert successor_event.state == "pending"
        assert epoch.external_effects_enabled is False
        assert ProjectionService(session).claim_next(
            worker_id="recovery-post-write-worker",
            now=WORKFLOW_NOW + timedelta(minutes=16),
            ttl=timedelta(minutes=1),
        ) is None



def test_rehydration_rejects_wrong_successor_or_recovery_identity(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        service, candidate_id, _ = _burn_rollback(
            session, ids, context, task_id, dish_release=generation.dish_release
        )
        candidate = service._candidate(candidate_id)
    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        state = _physical_state()
        control = _control_for_candidate(context, ids, state, candidate)
        promote_restored_generation(
            session, control, recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            uuid_factory=lambda: _next(ids),
        )
        with pytest.raises(RestoreControlError, match="lineage"):
            rehydrate_restored_generation(
                session,
                replace(control, generation_id=uuid.uuid4()),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=9),
            )
        with pytest.raises(RestoreControlError, match="recovery authority"):
            rehydrate_restored_generation(
                session,
                replace(control, bootstrap_capability_digest=hashlib.sha256(b"wrong").digest()),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=9),
            )
