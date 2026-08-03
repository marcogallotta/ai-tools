"""Runtime wiring rehearsal: exercise the projection worker against real PostgreSQL.

This does not repeat the outbox claim/adjudicate correctness already covered by
tests/postgresql/test_stage5_transition_projection.py (SQLite-backed, source
contract). It proves the *process*: dish_pg.projection_worker driving the real
ProjectionService across separately committed transactions, using a real
connection pool and real row locks, matching how the deployed worker will run.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.projection_worker import ExternalAttempt, ExternalObservation, ProjectionWorker
from dish_pg.transition import ProjectionService
from dish_pg.workflow import WorkflowAuthorityService
from tests.support.postgresql.core import NOW, _bootstrap_registry, _import_one, core_db
from tests.support.postgresql.workflow import _admit, _execution, _next, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


class _StubAdapter:
    """Records every call it receives; always confirms the intended effect."""

    def __init__(self) -> None:
        self.prepared: list[uuid.UUID] = []
        self.observed: list[uuid.UUID] = []

    def prepare(self, claim) -> ExternalAttempt:
        self.prepared.append(claim.event_id)
        return ExternalAttempt(
            request_identity=f"stub-request:{claim.event_id}",
            request_payload=dict(claim.payload),
            intended_external_id="123456789",
        )

    def attempt_and_observe(self, claim, attempt: ExternalAttempt) -> ExternalObservation:
        self.observed.append(claim.event_id)
        return ExternalObservation(
            observed_applied=True,
            observed_identity=claim.idempotency_key,
            reread_complete=True,
            evidence={"gid": "123456789"},
        )


def _seed_pending_event(factory, ids) -> tuple[uuid.UUID, uuid.UUID]:
    """Bootstrap one task with one pending update_task_document outbox event."""

    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task_id = _next(ids)
        _import_one(session, ids, context, task_id=task_id)
        generation_id = context["generation_id"]

        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
        _register_run(session, generation_id=generation_id, run_id=run_id)
        _admit(
            workflow,
            request_id=request_id,
            generation_id=generation_id,
            run_id=run_id,
            command="update",
            payload={"task_id": str(task_id)},
        )
        _execution(
            workflow,
            execution_id=execution_id,
            request_id=request_id,
            generation_id=generation_id,
            task_id=task_id,
            binding_id=context["binding_id"],
            command="update",
        )
        workflow.repo.claim_execution(
            execution_id=execution_id,
            claimant=f"owner-1:{run_id}",
            claim_token=_next(ids),
            now=NOW,
            ttl=timedelta(minutes=2),
        )

        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        projection.activate_epoch(
            generation_id=generation_id, activation_reason="worker rehearsal", created_at=NOW,
            external_effects_enabled=True,
        )
        event_id = projection.record(
            generation_id=generation_id,
            execution_id=execution_id,
            task_id=task_id,
            event_type="update_task_document",
            payload={"content_version_id": "v2"},
            created_at=NOW,
        )
        return event_id, task_id


def test_projection_worker_drains_one_pending_event_against_real_postgresql(core_db) -> None:
    factory, ids = core_db
    event_id, _task_id = _seed_pending_event(factory, ids)

    adapter = _StubAdapter()
    worker = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="projection-worker-rehearsal",
        claim_ttl=timedelta(minutes=2),
        clock=lambda: NOW,
    )

    assert worker.run_once() is True
    assert adapter.prepared == [event_id]
    assert adapter.observed == [event_id]

    with session_scope(factory) as session:
        row = session.get(tx.ProjectionOutboxEvent, event_id)
        assert row.state == "applied"
        assert row.claim_owner is None
        assert row.claim_token is None
        assert row.terminal_at is not None

    # Nothing left to claim: a second run finds no pending work.
    assert worker.run_once() is False


def test_projection_worker_never_claims_real_shadow_evaluator_outbox(core_db) -> None:
    from dish_pg.shadow_worker import CommandPortShadowEvaluator
    from dish_pg.transition import ShadowService

    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        generation_id = context["generation_id"]
        run_id = _next(ids)
        request_id = _next(ids)
        _register_run(session, generation_id=generation_id, run_id=run_id)

        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        epoch = projection.activate_epoch(
            generation_id=generation_id,
            activation_reason="native shadow-origin isolation",
            created_at=NOW,
            external_effects_enabled=True,
        )
        shadow = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = shadow.create_baseline(
            generation_id=generation_id,
            source_generation_identity="legacy-native",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = shadow.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="create",
            source_request_identity=str(request_id),
            canonical_input={
                "command": "create",
                "arguments": {"title": "Native shadow only"},
            },
            source_outcome={"ok": True},
            source_post_state={"captured": True},
            principal={
                "owner_id": "owner-1",
                "principal_class": "agent",
                "run_id": str(run_id),
            },
            pinned_inputs={"rollout_mode": "execute"},
            capture_qualification="execute",
            source_authority_generation="legacy-native",
            rollout_sequence=1,
            captured_at=NOW,
        )
        target = CommandPortShadowEvaluator(
            cursor_secret=b"native-shadow-cursor-secret-32b!"
        ).evaluate(session, envelope)
        event = session.scalar(
            select(tx.ProjectionOutboxEvent).where(
                tx.ProjectionOutboxEvent.origin == "shadow"
            )
        )
        assert target["ok"] is True
        assert epoch.external_effects_enabled is True
        assert event is not None
        event_id = event.projection_event_id

    adapter = _StubAdapter()
    worker = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="projection-worker-shadow-isolation",
        claim_ttl=timedelta(minutes=2),
        clock=lambda: NOW,
    )

    assert worker.run_once() is False
    assert adapter.prepared == []
    assert adapter.observed == []
    with session_scope(factory) as session:
        row = session.get(tx.ProjectionOutboxEvent, event_id)
        assert row.origin == "shadow"
        assert row.state == "pending"
        assert row.claim_owner is None
        assert row.claim_token is None
