from __future__ import annotations
import uuid
from datetime import timedelta
import pytest
from sqlalchemy import select
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_worker import (
    ShadowIdentityMappingError,
    ShadowWorker,
    _translate_workflow_identifiers,
)
from dish_pg.transition import ShadowService
from dish_pg.workflow import sha256_json
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.command import (
    _add_verification_queue,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db

from tests.support.postgresql.dark_launch_shadow_worker import (
    Evaluator,
    _spool,
    _real_verification_target,
)


def test_real_shadow_evaluator_tags_outbox_and_projection_claim_refuses_it(workflow_db):
    from dish_pg.shadow_worker import CommandPortShadowEvaluator
    from dish_pg.transition import ProjectionService

    factory, ids, context, _task = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        epoch = projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="prove shadow origin isolation",
            created_at=NOW,
            external_effects_enabled=True,
        )
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = ShadowService(session, uuid_factory=lambda: _next(ids)).capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="create",
            source_request_identity=str(request_id),
            canonical_input={"command": "create", "arguments": {"title": "Shadow only"}},
            source_outcome={"ok": True},
            source_post_state={"captured": True},
            principal={
                "owner_id": "owner-1",
                "principal_class": "agent",
                "run_id": str(run_id),
            },
            pinned_inputs={"rollout_mode": "execute"},
            capture_qualification="execute",
            source_authority_generation="legacy-1",
            rollout_sequence=1,
            captured_at=NOW,
        )

        target = CommandPortShadowEvaluator(cursor_secret=b"shadow-test-cursor-secret-32bytes!").evaluate(
            session, envelope
        )
        event = session.scalar(select(tx.ProjectionOutboxEvent))
        registered_run = session.scalar(select(wf.ServiceRun))
        request = session.scalar(select(wf.ServiceRequest))

        assert target["ok"] is True
        assert registered_run is not None
        assert registered_run.run_id != run_id
        assert registered_run.owner_id == "owner-1"
        assert registered_run.agent == "service"
        assert request is not None
        assert request.run_id == registered_run.run_id
        assert request.request_id != request_id
        assert epoch.external_effects_enabled is True
        assert event is not None
        assert event.origin == "shadow"
        assert event.state == "pending"
        assert projection.claim_next(
            worker_id="projection-worker",
            now=NOW,
            ttl=timedelta(minutes=2),
        ) is None

def test_shadow_identifier_translation_uses_prior_comparison_bindings(workflow_db):
    factory, ids, context, _task = workflow_db
    source_operation, target_operation = uuid.uuid4(), uuid.uuid4()
    source_lease, target_lease = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        prior = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="source-start",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={
                "submission_id": str(source_operation),
                "data": {"operation_id": str(source_operation), "lease_id": str(source_lease)},
            },
            source_post_state={"phase": "research"},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        target_result = {
            "evidence_schema_version": 2,
            "response": {
                "ok": True,
                "command": "start",
                "code": "OK",
                "http_status": 200,
                "data": {"operation_id": str(target_operation), "lease_id": str(target_lease)},
                "retryable": False,
            },
        }
        session.add(
            tx.ShadowComparison(
                comparison_id=_next(ids),
                envelope_id=prior.envelope_id,
                target_result=target_result,
                target_result_sha256=sha256_json(target_result),
                parity_class="semantic",
                differences=[],
                comparator_release="test",
                compared_at=NOW,
            )
        )
        current = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="renew-lease",
            source_request_identity="source-renew",
            canonical_input={"command": "renew-lease", "arguments": {}},
            source_outcome={"ok": True},
            source_post_state={"phase": "research"},
            rollout_sequence=2,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        translated = _translate_workflow_identifiers(
            session,
            current,
            {"submission_id": str(source_operation), "lease_id": str(source_lease)},
        )
        assert translated == {
            "submission_id": str(target_operation),
            "lease_id": str(target_lease),
        }
        with pytest.raises(ShadowIdentityMappingError, match="no unique target operation"):
            _translate_workflow_identifiers(
                session,
                current,
                {"submission_id": str(uuid.uuid4())},
            )

def test_real_shadow_evaluator_translates_verification_continuation_before_dispatch(workflow_db):
    from dish_pg.shadow_worker import CommandPortShadowEvaluator

    factory, ids, context, task_id = workflow_db
    source_operation = uuid.uuid4()
    source_cycle = uuid.uuid4()
    with session_scope(factory) as session:
        target_operation, target_cycle = _real_verification_target(
            session, ids, context, task_id
        )
        assert str(source_operation) != target_operation
        assert str(source_cycle) != target_cycle

        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        prior = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="legacy-verification-start",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={
                "data": {
                    "operation_id": str(source_operation),
                    "cycle_id": str(source_cycle),
                }
            },
            source_post_state={"phase": "await_verification"},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        target_result = {
            "evidence_schema_version": 2,
            "response": {
                "ok": True,
                "data": {
                    "operation_id": target_operation,
                    "cycle_id": target_cycle,
                },
            },
        }
        session.add(
            tx.ShadowComparison(
                comparison_id=_next(ids),
                envelope_id=prior.envelope_id,
                target_result=target_result,
                target_result_sha256=sha256_json(target_result),
                parity_class="semantic",
                differences=[],
                comparator_release="test",
                compared_at=NOW,
            )
        )
        current = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="legacy-verification-continuation",
            canonical_input={
                "command": "start",
                "arguments": {
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "codex",
                    "independence_attestation": (
                        "I independently inspected this exact candidate."
                    ),
                    "target_operation_id": str(source_operation),
                    "target_cycle_id": str(source_cycle),
                },
            },
            source_outcome={"ok": True},
            source_post_state={"phase": "await_verification"},
            principal={
                "owner_id": "verifier-owner",
                "principal_class": "agent",
                "run_id": str(uuid.uuid4()),
            },
            pinned_inputs={"rollout_mode": "execute"},
            rollout_sequence=2,
            source_authority_generation="legacy-1",
            capture_qualification="execute",
            captured_at=NOW,
        )

        target = CommandPortShadowEvaluator(
            cursor_secret=b"shadow-test-cursor-secret-32bytes!"
        ).evaluate(session, current)

        assert target["response"]["ok"] is True
        assert target["response"]["data"]["operation_id"] == target_operation
        assert target["response"]["data"]["cycle_id"] == target_cycle

def test_real_shadow_evaluator_rejects_unmapped_verification_continuation(workflow_db):
    from dish_pg.shadow_worker import CommandPortShadowEvaluator

    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _real_verification_target(session, ids, context, task_id)
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        current = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="unmapped-verification-continuation",
            canonical_input={
                "command": "start",
                "arguments": {
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "codex",
                    "independence_attestation": (
                        "I independently inspected this exact candidate."
                    ),
                    "target_operation_id": str(uuid.uuid4()),
                    "target_cycle_id": str(uuid.uuid4()),
                },
            },
            source_outcome={"ok": True},
            source_post_state={"phase": "await_verification"},
            principal={
                "owner_id": "verifier-owner",
                "principal_class": "agent",
                "run_id": str(uuid.uuid4()),
            },
            pinned_inputs={"rollout_mode": "execute"},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            capture_qualification="execute",
            captured_at=NOW,
        )

        with pytest.raises(
            ShadowIdentityMappingError,
            match="no unique target operation binding for captured field target_operation_id",
        ):
            CommandPortShadowEvaluator(
                cursor_secret=b"shadow-test-cursor-secret-32bytes!"
            ).evaluate(session, current)

def test_shadow_identifier_translation_rejects_mismatch_and_non_bijective_bindings(workflow_db):
    factory, ids, context, _task = workflow_db
    shared_target = uuid.uuid4()
    source_values = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    with session_scope(factory) as session:
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        for sequence, source_value in enumerate(source_values, 1):
            prior = service.capture_envelope(
                shadow_baseline_id=baseline.shadow_baseline_id,
                command_name="start",
                source_request_identity=f"source-{sequence}",
                canonical_input={"command": "start", "arguments": {}},
                source_outcome={"submission_id": str(source_value)},
                source_post_state={"phase": "research"},
                rollout_sequence=sequence,
                source_authority_generation="legacy-1",
                captured_at=NOW,
            )
            target_result = {
                "evidence_schema_version": 2,
                "response": {"data": {"operation_id": str(shared_target)}},
            }
            session.add(
                tx.ShadowComparison(
                    comparison_id=_next(ids),
                    envelope_id=prior.envelope_id,
                    target_result=target_result,
                    target_result_sha256=sha256_json(target_result),
                    parity_class="mismatch" if sequence == 1 else "semantic",
                    differences=[],
                    comparator_release="test",
                    compared_at=NOW,
                )
            )
        current = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="prepare",
            source_request_identity="current",
            canonical_input={"command": "prepare", "arguments": {}},
            source_outcome={"ok": True},
            source_post_state={"phase": "research"},
            rollout_sequence=4,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        with pytest.raises(ShadowIdentityMappingError):
            _translate_workflow_identifiers(
                session, current, {"submission_id": str(source_values[0])}
            )
        for source_value in source_values[1:]:
            with pytest.raises(ShadowIdentityMappingError):
                _translate_workflow_identifiers(
                    session, current, {"submission_id": str(source_value)}
                )

def test_real_shadow_evaluator_compares_legacy_start_semantically(workflow_db):
    from dish_pg.shadow_worker import CommandPortShadowEvaluator
    from tests.support.postgresql.core import HASH_A

    factory, ids, context, _task = workflow_db
    source_operation = uuid.uuid4()
    source_pre = {
        "task_gid": "123456789",
        "selected_tables": ["task_content_state", "operations", "service_leases"],
        "tables": {
            "task_content_state": [{
                "last_confirmed_identity": HASH_A,
                "last_confirmed_title": "[ready] Exact imported task",
                "last_confirmed_notes": "Canonical body\n---\nStatus: ready\n",
                "schema_version": "1",
            }],
            "operations": [],
            "service_leases": [],
        },
    }
    source_post = {
        **source_pre,
        "tables": {
            **source_pre["tables"],
            "operations": [{
                "operation_kind": "initial",
                "status": "open",
                "phase": "prepare_required",
                "terminal_outcome": None,
            }],
            "service_leases": [{
                "released_at": None,
                "lease_kind": "actor",
                "actor_attempt_seq": 1,
            }],
        },
    }
    source_outcome = {
        "ok": True,
        "command": "start",
        "code": "OK",
        "task_gid": "123456789",
        "submission_id": str(source_operation),
        "state": "open",
        "retryable": False,
        "allowed_actions": ["prepare"],
        "data": {},
        "errors": [],
    }
    claim_token = uuid.uuid4()
    with session_scope(factory) as session:
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="legacy-start",
            canonical_input={
                "command": "start",
                "arguments": {"task_gid": "123456789", "kind": "initial", "agent": "service"},
            },
            source_outcome=source_outcome,
            source_pre_state=source_pre,
            source_post_state=source_post,
            principal={"owner_id": "owner-1", "principal_class": "agent", "run_id": str(uuid.uuid4())},
            pinned_inputs={"rollout_mode": "execute"},
            source_effects={},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            capture_qualification="execute",
            captured_at=NOW,
        )
        delivery = service.claim_delivery(
            worker_id="shadow-1",
            claim_token=claim_token,
            now=NOW,
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        target = CommandPortShadowEvaluator(
            cursor_secret=b"shadow-test-cursor-secret-32bytes!"
        ).evaluate(session, envelope)
        comparison = service.compare_delivery(
            delivery_id=delivery.delivery_id,
            claim_token=claim_token,
            claim_revision=delivery.delivery_revision,
            worker_id="shadow-1",
            target_result=target,
            comparator_release="test",
            compared_at=NOW,
        )
        assert comparison.parity_class == "semantic"
        assert comparison.differences == []
