from __future__ import annotations
import uuid
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_worker import (
    ShadowIdentityMappingError,
    ShadowWorker,
    _record_created_task_alias,
    _shadow_uuid,
    _translate_prepare_candidate,
    _translate_workflow_identifiers,
)
from dish_pg.transition import ShadowService
from dish_pg.workflow import sha256_json
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.command import (
    _add_destination_section,
    _add_verification_queue,
    _call,
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


def test_shadow_prepare_uses_committed_source_candidate() -> None:
    envelope = SimpleNamespace(
        command_name="prepare",
        source_post_state={
            "tables": {
                "task_content_state": [
                    {
                        "task_gid": "123",
                        "last_confirmed_title": "[pending-verification] Candidate",
                        "last_confirmed_notes": "Body\n---\nStatus: pending-verification\n",
                    }
                ]
            }
        },
    )
    translated = _translate_prepare_candidate(
        envelope,
        {"task_gid": "123", "file_text": "[ready] Candidate\nold body\n"},
    )
    assert translated["file_text"] == (
        "[pending-verification] Candidate\n"
        "Body\n---\nStatus: pending-verification\n"
    )


@pytest.mark.parametrize("source_outcome", [{}, {"data": {}}, {"data": {"task_gid": "  "}}])
def test_shadow_create_requires_captured_source_task_gid(source_outcome) -> None:
    with pytest.raises(
        ShadowIdentityMappingError,
        match="source outcome has no task_gid",
    ):
        _record_created_task_alias(
            None,
            envelope=SimpleNamespace(source_outcome=source_outcome),
            result=SimpleNamespace(data={}),
        )


def _add_imported_operation(session, *, context, task_id, operation_id, import_run_id=None):
    session.add(
        wf.WorkflowOperation(
            operation_id=operation_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            kind="planning",
            lifecycle="completed",
            phase="terminal",
            persisted_actions=[],
            import_run_id=import_run_id or context["import_run_id"],
            creation_request_id=None,
            creation_execution_id=None,
            contract_binding_id=context["binding_id"],
            predecessor_operation_id=None,
            terminal_outcome="planning_handoff_confirmed",
            operation_revision=1,
            created_at=NOW,
            terminal_at=NOW,
        )
    )
    session.flush()


def _captured_envelope(session, *, context, source_generation, source_commit, identity):
    service = ShadowService(session)
    baseline = service.create_baseline(
        generation_id=context["generation_id"],
        source_generation_identity=source_generation,
        source_commit=source_commit,
        created_at=NOW,
    )
    return service.capture_envelope(
        shadow_baseline_id=baseline.shadow_baseline_id,
        command_name="start",
        source_request_identity=identity,
        canonical_input={"command": "start", "arguments": {}},
        source_outcome={"ok": True},
        source_post_state={"phase": "research"},
        rollout_sequence=1,
        source_authority_generation=source_generation,
        captured_at=NOW,
    )


def test_shadow_identifier_translation_accepts_exact_import_lineage(workflow_db):
    factory, ids, context, task_id = workflow_db
    operation_id = uuid.uuid4()
    with session_scope(factory) as session:
        run = session.get(models.ImportRun, context["import_run_id"])
        assert run is not None
        _add_imported_operation(session, context=context, task_id=task_id, operation_id=operation_id)
        verification_section_id = _add_destination_section(
            session, ids, context, external_id="1217084805070799"
        )
        admin_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="marco",
        )
        revised = _port(session, ids).execute(
            _call(
                "revise-section-registry",
                run_id=admin_run,
                request_id=_next(ids),
                principal="admin",
                owner="Marco",
                arguments={
                    "research_queue_section_id": str(context["section_id"]),
                    "verification_queue_section_id": str(verification_section_id),
                },
            )
        )
        assert revised.ok is True
        envelope = _captured_envelope(
            session,
            context=context,
            source_generation=run.legacy_generation_id,
            source_commit=run.source_commit,
            identity="matching-imported-operation",
        )
        assert _translate_workflow_identifiers(
            session, envelope, {"submission_id": str(operation_id)}
        ) == {"submission_id": str(operation_id)}


def test_shadow_identifier_translation_rejects_live_identity_without_binding(workflow_db):
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run = session.get(models.ImportRun, context["import_run_id"])
        assert run is not None
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        operation_id = _start_initial(_port(session, ids), ids, task_id=task_id, run_id=run_id).data["operation_id"]
        envelope = _captured_envelope(
            session,
            context=context,
            source_generation=run.legacy_generation_id,
            source_commit=run.source_commit,
            identity="live-operation-without-binding",
        )
        with pytest.raises(ShadowIdentityMappingError, match="no unique target operation"):
            _translate_workflow_identifiers(session, envelope, {"submission_id": str(operation_id)})


def test_shadow_identifier_translation_rejects_different_import_lineage(workflow_db):
    factory, ids, context, task_id = workflow_db
    operation_id = uuid.uuid4()
    with session_scope(factory) as session:
        run_a = session.get(models.ImportRun, context["import_run_id"])
        assert run_a is not None
        run_b_id = _next(ids)
        session.add(
            models.ImportRun(
                import_run_id=run_b_id,
                source_commit=run_a.source_commit,
                source_release=run_a.source_release,
                legacy_generation_id=run_a.legacy_generation_id,
                baseline_high_water_mark="other-corpus",
                source_bundle_sha256="b" * 64,
                status="complete",
                started_at=NOW,
                completed_at=NOW,
                provenance={"capture": "other-corpus"},
            )
        )
        _add_imported_operation(
            session,
            context=context,
            task_id=task_id,
            operation_id=operation_id,
            import_run_id=run_b_id,
        )
        envelope = _captured_envelope(
            session,
            context=context,
            source_generation=run_a.legacy_generation_id,
            source_commit=run_a.source_commit,
            identity="wrong-import-lineage",
        )
        with pytest.raises(ShadowIdentityMappingError, match="no unique target operation"):
            _translate_workflow_identifiers(session, envelope, {"submission_id": str(operation_id)})


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
            source_outcome={"ok": True, "data": {"task_gid": "shadow-created-1"}},
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

        evaluator = CommandPortShadowEvaluator(
            cursor_secret=b"shadow-test-cursor-secret-32bytes!"
        )
        target = evaluator.evaluate(session, envelope)
        replayed = evaluator.evaluate(session, envelope)
        event = session.scalar(select(tx.ProjectionOutboxEvent))
        alias = session.scalar(
            select(models.TaskExternalAlias).where(
                models.TaskExternalAlias.external_id == "shadow-created-1"
            )
        )
        registered_run = session.scalar(select(wf.ServiceRun))
        request = session.scalar(select(wf.ServiceRequest))

        assert target["ok"] is True
        assert replayed["ok"] is True
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
        assert alias is not None
        assert alias.task_id == event.task_id
        assert alias.origin == "projection"
        assert alias.import_run_id is None
        assert alias.projection_event_id == event.projection_event_id
        assert len(
            list(
                session.scalars(
                    select(models.TaskExternalAlias).where(
                        models.TaskExternalAlias.external_id == "shadow-created-1"
                    )
                )
            )
        ) == 1
        assert projection.claim_next(
            worker_id="projection-worker",
            now=NOW,
            ttl=timedelta(minutes=2),
        ) is None

def _source_task_gid(session, task_id) -> str:
    value = session.scalar(
        select(models.TaskExternalAlias.external_id).where(
            models.TaskExternalAlias.task_id == task_id,
            models.TaskExternalAlias.external_system == "asana",
            models.TaskExternalAlias.state == "active",
        )
    )
    assert value
    return str(value)


def _source_creator_request_row(*, request_id, operation_id, task_gid, kind):
    return {
        "request_id": request_id,
        "owner_id": "owner-1",
        "run_id": "source-author-run",
        "command": "start",
        "request_hash": "source-hash",
        "status": "completed",
        "operation_id": str(operation_id),
        "task_gid": task_gid,
        "result_json": {
            "ok": True,
            "command": "start",
            "task_gid": task_gid,
            "submission_id": str(operation_id),
            "data": {"operation_id": str(operation_id), "operation_kind": kind},
        },
        "resolution_result_json": None,
        "created_at": (NOW - timedelta(seconds=1)).isoformat(),
        "completed_at": (NOW + timedelta(seconds=1)).isoformat(),
    }


def test_shadow_identifier_translation_uses_envelope_local_operation_lineage(workflow_db):
    factory, ids, context, task_id = workflow_db
    source_operation = uuid.uuid4()
    source_request_id = "legacy-start-request"
    source_run_id = "source-author-run"
    with session_scope(factory) as session:
        task_gid = _source_task_gid(session, task_id)
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="prepare",
            source_request_identity="legacy-prepare",
            canonical_input={"command": "prepare", "arguments": {}},
            source_outcome={"ok": True},
            source_pre_state={
                "selected_tables": ["operations", "service_requests"],
                "lineage_scope": {
                    "operation_ids": [str(source_operation)],
                    "explicit_request_ids": [],
                },
                "tables": {
                    "operations": [{
                        "operation_id": str(source_operation),
                        "task_gid": task_gid,
                        "operation_kind": "initial",
                        "created_at": NOW.isoformat(),
                    }],
                    "service_requests": [_source_creator_request_row(
                        request_id=source_request_id,
                        operation_id=source_operation,
                        task_gid=task_gid,
                        kind="initial",
                    )],
                },
            },
            source_post_state={"phase": "research"},
            rollout_sequence=2,
            source_authority_generation="legacy-1",
            pinned_inputs={"capture_schema": 3, "rollout_mode": "execute"},
            captured_at=NOW,
        )

        target_run_id = _shadow_uuid(
            envelope, label="run", value=f"owner-1:{source_run_id}"
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=target_run_id,
            owner="owner-1",
            agent="claude",
        )
        target_start = _port(session, ids).execute(
            _call(
                "start",
                run_id=target_run_id,
                request_id=_shadow_uuid(envelope, label="request", value=source_request_id),
                owner="owner-1",
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert target_start.ok

        prior = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="misleading-sibling",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={"submission_id": str(source_operation)},
            source_post_state={"phase": "research"},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        misleading = {
            "evidence_schema_version": 2,
            "response": {"data": {"operation_id": str(uuid.uuid4())}},
        }
        session.add(
            tx.ShadowComparison(
                comparison_id=_next(ids),
                envelope_id=prior.envelope_id,
                target_result=misleading,
                target_result_sha256=sha256_json(misleading),
                parity_class="semantic",
                differences=[],
                comparator_release="test",
                compared_at=NOW,
            )
        )

        assert _translate_workflow_identifiers(
            session, envelope, {"submission_id": str(source_operation)}
        ) == {"submission_id": target_start.data["operation_id"]}


def test_shadow_identifier_translation_resolves_successor_operation_from_local_succession(workflow_db):
    factory, ids, context, task_id = workflow_db
    source_operation = uuid.uuid4()
    source_successor = uuid.uuid4()
    source_request_id = "legacy-abandonment-source-start"
    source_run_id = "source-author-run"
    with session_scope(factory) as session:
        task_gid = _source_task_gid(session, task_id)
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
            source_request_identity="legacy-successor-verification",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={"ok": True},
            source_pre_state={
                "selected_tables": [
                    "operations", "service_requests", "operation_successions",
                ],
                "lineage_scope": {
                    "operation_ids": [str(source_operation), str(source_successor)],
                    "explicit_request_ids": [],
                },
                "tables": {
                    "operations": [
                        {
                            "operation_id": str(source_operation),
                            "task_gid": task_gid,
                            "operation_kind": "initial",
                            "created_at": NOW.isoformat(),
                        },
                        {
                            "operation_id": str(source_successor),
                            "task_gid": task_gid,
                            "operation_kind": "initial",
                            "created_at": (NOW + timedelta(seconds=2)).isoformat(),
                        },
                    ],
                    "service_requests": [_source_creator_request_row(
                        request_id=source_request_id,
                        operation_id=source_operation,
                        task_gid=task_gid,
                        kind="initial",
                    )],
                    "operation_successions": [{
                        "succession_id": str(uuid.uuid4()),
                        "task_gid": task_gid,
                        "source_operation_id": str(source_operation),
                        "successor_operation_id": str(source_successor),
                    }],
                },
            },
            source_post_state={"phase": "await_verification"},
            pinned_inputs={"capture_schema": 3, "rollout_mode": "execute"},
            rollout_sequence=2,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )

        author_run = _shadow_uuid(envelope, label="run", value=f"owner-1:{source_run_id}")
        verifier_run = _next(ids)
        admin_run = _next(ids)
        _register_run(
            session, generation_id=context["generation_id"], run_id=author_run,
            owner="owner-1", agent="claude",
        )
        _register_run(
            session, generation_id=context["generation_id"], run_id=verifier_run,
            owner="old-verifier", agent="gpt",
        )
        _register_run(
            session, generation_id=context["generation_id"], run_id=admin_run,
            owner="Marco", agent="claude",
        )
        _add_verification_queue(session, ids, context)
        port = _port(session, ids)
        started = port.execute(
            _call(
                "start",
                run_id=author_run,
                request_id=_shadow_uuid(envelope, label="request", value=source_request_id),
                owner="owner-1",
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert started.ok
        _prepare_for_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"],
            run_id=author_run,
        )
        verification = _start_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"],
            run_id=verifier_run, owner="old-verifier", agent="gpt",
        )
        abandoned = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "lease_id": verification.data["lease_id"],
                    "reason": "verifier permanently unavailable",
                },
            )
        )
        assert abandoned.ok
        edge = session.scalar(select(wf.OperationSuccessionEdge))
        assert edge is not None

        assert _translate_workflow_identifiers(
            session, envelope, {"target_operation_id": str(source_successor)}
        ) == {"target_operation_id": str(edge.successor_operation_id)}


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

def test_shadow_identifier_translation_ignores_sibling_comparison_bindings_without_local_lineage(workflow_db):
    factory, ids, context, _task = workflow_db
    source_operation = uuid.uuid4()
    target_operation = uuid.uuid4()
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
            source_outcome={"submission_id": str(source_operation)},
            source_post_state={"phase": "research"},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        target_result = {
            "evidence_schema_version": 2,
            "response": {"data": {"operation_id": str(target_operation)}},
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
            command_name="prepare",
            source_request_identity="current",
            canonical_input={"command": "prepare", "arguments": {}},
            source_outcome={"ok": True},
            source_post_state={"phase": "research"},
            rollout_sequence=2,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )

        with pytest.raises(ShadowIdentityMappingError, match="no unique target operation"):
            _translate_workflow_identifiers(
                session, current, {"submission_id": str(source_operation)}
            )


def _add_irrelevant_sibling_deliveries(session, service, baseline, ids, *, current_sequence):
    """Populate failed/pending siblings around a current envelope; resolvers must ignore them."""
    for sequence, state in (
        (current_sequence - 2, "failed"),
        (current_sequence - 1, "pending"),
        (current_sequence + 1, "pending"),
    ):
        sibling = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity=f"irrelevant-sibling-{current_sequence}-{sequence}",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={"ok": state != "failed"},
            source_post_state={"phase": "irrelevant"},
            rollout_sequence=sequence,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        delivery = session.scalar(
            select(tx.ShadowDelivery).where(tx.ShadowDelivery.envelope_id == sibling.envelope_id)
        )
        assert delivery is not None
        if state == "failed":
            delivery.state = "failed"
            delivery.last_error = "irrelevant sibling failed"
            delivery.terminal_at = NOW


def test_patch_b_actor_lease_resolves_from_envelope_local_lineage_with_bad_siblings(workflow_db):
    factory, ids, context, task_id = workflow_db
    source_operation = uuid.uuid4()
    source_lease = uuid.uuid4()
    source_request_id = "legacy-lease-source-start"
    source_run_id = "source-author-run"
    with session_scope(factory) as session:
        task_gid = _source_task_gid(session, task_id)
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="renew-lease",
            source_request_identity="legacy-renew",
            canonical_input={"command": "renew-lease", "arguments": {}},
            source_outcome={"ok": True},
            source_pre_state={
                "selected_tables": ["operations", "service_leases", "service_requests"],
                "lineage_scope": {
                    "operation_ids": [str(source_operation)],
                    "lease_ids": [str(source_lease)],
                    "cycle_ids": [],
                    "challenge_ids": [],
                    "abandonment_ids": [],
                    "explicit_request_ids": [],
                },
                "tables": {
                    "operations": [{
                        "operation_id": str(source_operation),
                        "task_gid": task_gid,
                        "operation_kind": "initial",
                        "created_at": NOW.isoformat(),
                    }],
                    "service_requests": [_source_creator_request_row(
                        request_id=source_request_id,
                        operation_id=source_operation,
                        task_gid=task_gid,
                        kind="initial",
                    )],
                    "service_leases": [{
                        "lease_id": str(source_lease),
                        "operation_id": str(source_operation),
                        "task_gid": task_gid,
                        "owner_id": "owner-1",
                        "run_id": source_run_id,
                        "lease_kind": "actor",
                        "actor_attempt_seq": 1,
                        "context_cycle_id": None,
                    }],
                },
            },
            source_post_state={"phase": "research"},
            rollout_sequence=3,
            source_authority_generation="legacy-1",
            pinned_inputs={"capture_schema": 3, "rollout_mode": "execute"},
            captured_at=NOW,
        )
        _add_irrelevant_sibling_deliveries(
            session, service, baseline, ids, current_sequence=3
        )

        target_run_id = _shadow_uuid(
            envelope, label="run", value=f"owner-1:{source_run_id}"
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=target_run_id,
            owner="owner-1",
            agent="claude",
        )
        target_start = _port(session, ids).execute(
            _call(
                "start",
                run_id=target_run_id,
                request_id=_shadow_uuid(envelope, label="request", value=source_request_id),
                owner="owner-1",
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert target_start.ok

        assert _translate_workflow_identifiers(
            session, envelope, {"lease_id": str(source_lease)}
        ) == {"lease_id": target_start.data["lease_id"]}


def test_patch_b_verification_cycle_resolves_from_envelope_local_lineage_with_bad_siblings(workflow_db):
    factory, ids, context, task_id = workflow_db
    source_operation = uuid.uuid4()
    source_cycle = uuid.uuid4()
    source_request_id = "legacy-cycle-source-start"
    source_run_id = "source-author-run"
    with session_scope(factory) as session:
        task_gid = _source_task_gid(session, task_id)
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
            source_request_identity="legacy-verification-continuation",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={"ok": True},
            source_pre_state={
                "selected_tables": ["operations", "service_requests", "verification_cycles"],
                "lineage_scope": {
                    "operation_ids": [str(source_operation)],
                    "lease_ids": [],
                    "cycle_ids": [str(source_cycle)],
                    "challenge_ids": [],
                    "abandonment_ids": [],
                    "explicit_request_ids": [],
                },
                "tables": {
                    "operations": [{
                        "operation_id": str(source_operation),
                        "task_gid": task_gid,
                        "operation_kind": "initial",
                        "created_at": NOW.isoformat(),
                    }],
                    "service_requests": [_source_creator_request_row(
                        request_id=source_request_id,
                        operation_id=source_operation,
                        task_gid=task_gid,
                        kind="initial",
                    )],
                    "verification_cycles": [{
                        "cycle_id": str(source_cycle),
                        "operation_id": str(source_operation),
                        "task_gid": task_gid,
                        "cycle_number": 1,
                    }],
                },
            },
            source_post_state={"phase": "await_verification"},
            rollout_sequence=3,
            source_authority_generation="legacy-1",
            pinned_inputs={"capture_schema": 3, "rollout_mode": "execute"},
            captured_at=NOW,
        )
        _add_irrelevant_sibling_deliveries(
            session, service, baseline, ids, current_sequence=3
        )

        author_run = _shadow_uuid(envelope, label="run", value=f"owner-1:{source_run_id}")
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=author_run,
            owner="owner-1",
            agent="claude",
        )
        _add_verification_queue(session, ids, context)
        port = _port(session, ids)
        started = port.execute(
            _call(
                "start",
                run_id=author_run,
                request_id=_shadow_uuid(envelope, label="request", value=source_request_id),
                owner="owner-1",
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert started.ok
        _prepare_for_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"], run_id=author_run
        )
        target_cycle = session.scalar(
            select(wf.VerificationCycle).where(
                wf.VerificationCycle.operation_id == uuid.UUID(started.data["operation_id"]),
                wf.VerificationCycle.cycle_sequence == 1,
            )
        )
        assert target_cycle is not None

        assert _translate_workflow_identifiers(
            session, envelope, {"target_cycle_id": str(source_cycle)}
        ) == {"target_cycle_id": str(target_cycle.cycle_id)}


def test_patch_b_planning_challenge_resolves_from_envelope_local_lineage_with_bad_siblings(workflow_db):
    factory, ids, context, task_id = workflow_db
    source_challenge = uuid.uuid4()
    source_issue_request = "legacy-planning-challenge-issue"
    source_owner = "owner-1"
    source_run = "source-planning-run"
    with session_scope(factory) as session:
        task_gid = _source_task_gid(session, task_id)
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
            source_request_identity="legacy-planning-confirmed",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={"ok": True},
            source_pre_state={
                "selected_tables": ["planning_intent_challenges", "service_requests"],
                "lineage_scope": {
                    "operation_ids": [],
                    "lease_ids": [],
                    "cycle_ids": [],
                    "challenge_ids": [str(source_challenge)],
                    "abandonment_ids": [],
                    "explicit_request_ids": [source_issue_request],
                },
                "tables": {
                    "planning_intent_challenges": [{
                        "challenge_id": str(source_challenge),
                        "created_request_id": source_issue_request,
                        "owner_id": source_owner,
                        "run_id": source_run,
                        "task_gid": task_gid,
                        "agent": "claude",
                        "target_hash": "source-target-hash",
                        "status": "issued",
                        "claimed_request_id": None,
                        "operation_id": None,
                    }],
                    "service_requests": [{
                        "request_id": source_issue_request,
                        "owner_id": source_owner,
                        "run_id": source_run,
                        "command": "start",
                        "status": "completed",
                        "operation_id": None,
                        "task_gid": task_gid,
                    }],
                },
            },
            source_post_state={"phase": "planning"},
            rollout_sequence=3,
            source_authority_generation="legacy-1",
            pinned_inputs={"capture_schema": 3, "rollout_mode": "execute"},
            captured_at=NOW,
        )
        _add_irrelevant_sibling_deliveries(
            session, service, baseline, ids, current_sequence=3
        )

        target_run = _shadow_uuid(envelope, label="run", value=f"{source_owner}:{source_run}")
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=target_run,
            owner=source_owner,
            agent="claude",
        )
        issued = _port(session, ids).execute(
            _call(
                "start",
                run_id=target_run,
                request_id=_shadow_uuid(envelope, label="request", value=source_issue_request),
                owner=source_owner,
                arguments={"task_id": str(task_id), "kind": "planning", "agent": "claude"},
            )
        )
        assert not issued.ok and issued.code == "CONFIRMATION_REQUIRED"

        assert _translate_workflow_identifiers(
            session, envelope, {"intent_challenge_id": str(source_challenge)}
        ) == {"intent_challenge_id": issued.data["intent_challenge_id"]}


def test_patch_b_abandonment_resolves_from_envelope_local_lineage_with_bad_siblings(workflow_db):
    factory, ids, context, task_id = workflow_db
    source_operation = uuid.uuid4()
    source_lease = uuid.uuid4()
    source_abandonment = uuid.uuid4()
    source_start_request = "legacy-abandon-source-start"
    source_abandon_request = "legacy-abandon-request"
    source_execution = "legacy-abandon-execution"
    source_actor_owner = "owner-1"
    source_actor_run = "source-actor-run"
    source_admin_run = "source-admin-run"
    with session_scope(factory) as session:
        task_gid = _source_task_gid(session, task_id)
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="reconcile-abandonment",
            source_request_identity="legacy-reconcile-abandonment",
            canonical_input={"command": "reconcile-abandonment", "arguments": {}},
            source_outcome={"ok": True},
            source_pre_state={
                "selected_tables": [
                    "operations", "service_leases", "service_requests",
                    "abandonment_attempts", "audit_events", "operation_executions",
                ],
                "lineage_scope": {
                    "operation_ids": [str(source_operation)],
                    "lease_ids": [str(source_lease)],
                    "cycle_ids": [],
                    "challenge_ids": [],
                    "abandonment_ids": [str(source_abandonment)],
                    "explicit_request_ids": [source_abandon_request],
                },
                "tables": {
                    "operations": [{
                        "operation_id": str(source_operation),
                        "task_gid": task_gid,
                        "operation_kind": "initial",
                        "created_at": NOW.isoformat(),
                    }],
                    "service_leases": [{
                        "lease_id": str(source_lease),
                        "operation_id": str(source_operation),
                        "task_gid": task_gid,
                        "owner_id": source_actor_owner,
                        "run_id": source_actor_run,
                        "lease_kind": "actor",
                        "actor_attempt_seq": 1,
                        "context_cycle_id": None,
                    }],
                    "service_requests": [
                        _source_creator_request_row(
                            request_id=source_start_request,
                            operation_id=source_operation,
                            task_gid=task_gid,
                            kind="initial",
                        ),
                        {
                            "request_id": source_abandon_request,
                            "owner_id": "Marco",
                            "run_id": source_admin_run,
                            "command": "abandon-operation",
                            "status": "completed",
                            "operation_id": str(source_operation),
                            "task_gid": task_gid,
                        },
                    ],
                    "operation_executions": [{
                        "execution_id": source_execution,
                        "operation_id": str(source_operation),
                        "request_id": source_abandon_request,
                        "command": "abandon-operation",
                    }],
                    "audit_events": [{
                        "event_id": "source-abandon-audit",
                        "operation_id": str(source_operation),
                        "event_type": "operation.abandonment_started",
                        "operation_execution_id": source_execution,
                        "details": {
                            "abandonment_id": str(source_abandonment),
                            "source_lease_id": str(source_lease),
                        },
                    }],
                    "abandonment_attempts": [{
                        "abandonment_id": str(source_abandonment),
                        "task_gid": task_gid,
                        "source_operation_id": str(source_operation),
                        "source_lease_id": str(source_lease),
                        "abandoned_owner_id": source_actor_owner,
                        "abandoned_run_id": source_actor_run,
                        "attempt_cycle_id": None,
                    }],
                },
            },
            source_post_state={"phase": "abandonment"},
            rollout_sequence=3,
            source_authority_generation="legacy-1",
            pinned_inputs={"capture_schema": 3, "rollout_mode": "execute"},
            captured_at=NOW,
        )
        _add_irrelevant_sibling_deliveries(
            session, service, baseline, ids, current_sequence=3
        )

        actor_run = _shadow_uuid(envelope, label="run", value=f"{source_actor_owner}:{source_actor_run}")
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=actor_run,
            owner=source_actor_owner,
            agent="claude",
        )
        port = _port(session, ids)
        started = port.execute(
            _call(
                "start",
                run_id=actor_run,
                request_id=_shadow_uuid(envelope, label="request", value=source_start_request),
                owner=source_actor_owner,
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert started.ok
        admin_run = _shadow_uuid(envelope, label="run", value=f"Marco:{source_admin_run}")
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        abandoned = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_shadow_uuid(envelope, label="request", value=source_abandon_request),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "lease_id": started.data["lease_id"],
                    "reason": "run permanently unavailable",
                },
            )
        )
        assert abandoned.ok

        assert _translate_workflow_identifiers(
            session, envelope, {"abandonment_id": str(source_abandonment)}
        ) == {"abandonment_id": abandoned.data["abandonment_id"]}


def test_patch_b_abandonment_continuation_pair_uses_local_succession(workflow_db):
    factory, ids, context, task_id = workflow_db
    source_operation = uuid.uuid4()
    source_successor = uuid.uuid4()
    source_successor_cycle = uuid.uuid4()
    source_request_id = "legacy-abandonment-source-start"
    source_run_id = "source-author-run"
    with session_scope(factory) as session:
        task_gid = _source_task_gid(session, task_id)
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
            source_request_identity="legacy-successor-verification",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={"ok": True},
            source_pre_state={
                "selected_tables": [
                    "operations", "service_requests", "verification_cycles", "operation_successions",
                ],
                "lineage_scope": {
                    "operation_ids": [str(source_operation), str(source_successor)],
                    "lease_ids": [],
                    "cycle_ids": [str(source_successor_cycle)],
                    "challenge_ids": [],
                    "abandonment_ids": [],
                    "explicit_request_ids": [],
                },
                "tables": {
                    "operations": [
                        {
                            "operation_id": str(source_operation),
                            "task_gid": task_gid,
                            "operation_kind": "initial",
                            "created_at": NOW.isoformat(),
                        },
                        {
                            "operation_id": str(source_successor),
                            "task_gid": task_gid,
                            "operation_kind": "initial",
                            "created_at": (NOW + timedelta(seconds=2)).isoformat(),
                        },
                    ],
                    "service_requests": [_source_creator_request_row(
                        request_id=source_request_id,
                        operation_id=source_operation,
                        task_gid=task_gid,
                        kind="initial",
                    )],
                    "verification_cycles": [{
                        "cycle_id": str(source_successor_cycle),
                        "operation_id": str(source_successor),
                        "task_gid": task_gid,
                        "cycle_number": 2,
                    }],
                    "operation_successions": [{
                        "succession_id": str(uuid.uuid4()),
                        "task_gid": task_gid,
                        "source_operation_id": str(source_operation),
                        "successor_operation_id": str(source_successor),
                        "source_cycle_id": str(uuid.uuid4()),
                        "successor_cycle_id": str(source_successor_cycle),
                    }],
                },
            },
            source_post_state={"phase": "await_verification"},
            pinned_inputs={"capture_schema": 3, "rollout_mode": "execute"},
            rollout_sequence=3,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        _add_irrelevant_sibling_deliveries(
            session, service, baseline, ids, current_sequence=3
        )

        author_run = _shadow_uuid(envelope, label="run", value=f"owner-1:{source_run_id}")
        verifier_run = _next(ids)
        admin_run = _next(ids)
        _register_run(
            session, generation_id=context["generation_id"], run_id=author_run,
            owner="owner-1", agent="claude",
        )
        _register_run(
            session, generation_id=context["generation_id"], run_id=verifier_run,
            owner="old-verifier", agent="gpt",
        )
        _register_run(
            session, generation_id=context["generation_id"], run_id=admin_run,
            owner="Marco", agent="claude",
        )
        _add_verification_queue(session, ids, context)
        port = _port(session, ids)
        started = port.execute(
            _call(
                "start",
                run_id=author_run,
                request_id=_shadow_uuid(envelope, label="request", value=source_request_id),
                owner="owner-1",
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert started.ok
        _prepare_for_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"], run_id=author_run,
        )
        verification = _start_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"],
            run_id=verifier_run, owner="old-verifier", agent="gpt",
        )
        abandoned = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "lease_id": verification.data["lease_id"],
                    "reason": "verifier permanently unavailable",
                },
            )
        )
        assert abandoned.ok
        edge = session.scalar(select(wf.OperationSuccessionEdge))
        assert edge is not None and edge.prepared_cycle_id is not None

        assert _translate_workflow_identifiers(
            session,
            envelope,
            {
                "target_operation_id": str(source_successor),
                "target_cycle_id": str(source_successor_cycle),
            },
        ) == {
            "target_operation_id": str(edge.successor_operation_id),
            "target_cycle_id": str(edge.prepared_cycle_id),
        }


@pytest.mark.parametrize(
    ("field", "family"),
    [
        ("lease_id", "lease"),
        ("target_cycle_id", "verification_cycle"),
        ("intent_challenge_id", "planning_challenge"),
        ("abandonment_id", "abandonment"),
    ],
)
def test_patch_b_missing_or_historical_lineage_fails_closed(workflow_db, field, family):
    factory, ids, context, _task_id = workflow_db
    source_value = uuid.uuid4()
    with session_scope(factory) as session:
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        for schema, identity in ((3, "missing-local-lineage"), (2, "historical-envelope")):
            envelope = service.capture_envelope(
                shadow_baseline_id=baseline.shadow_baseline_id,
                command_name="start",
                source_request_identity=f"{identity}-{field}",
                canonical_input={"command": "start", "arguments": {}},
                source_outcome={"ok": True},
                source_pre_state={"selected_tables": [], "tables": {}},
                source_post_state={},
                rollout_sequence=None,
                source_authority_generation="legacy-1",
                pinned_inputs={"capture_schema": schema, "rollout_mode": "capture"},
                captured_at=NOW,
            )
            with pytest.raises(ShadowIdentityMappingError, match=f"no unique target {family}"):
                _translate_workflow_identifiers(session, envelope, {field: str(source_value)})

def test_real_shadow_evaluator_reports_missing_start_continuation_fields(workflow_db):
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
        assert comparison.parity_class == "mismatch"
        assert [item["axis"] for item in comparison.differences] == ["response"]
        response_difference = comparison.differences[0]
        assert response_difference["source"]["allowed_actions"] == ["prepare"]
        assert response_difference["target"]["allowed_actions"] == []
        assert response_difference["target"]["task"] is None
        assert response_difference["target"]["state"] is None
