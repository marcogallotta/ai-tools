from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

import dish_pg.transition as transition_module
from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from tests.support.postgresql.command import SECRET, _call
from tests.support.postgresql.projection_attempts import external_evidence, seed_events
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db


def _port(session, ids) -> PostgresCommandPort:
    return PostgresCommandPort(
        session,
        cursor_secret=SECRET,
        uuid_factory=lambda: _next(ids),
    )


def _archive(port, ids, task_id, admin_run):
    return port.execute(
        _call(
            "archive",
            run_id=admin_run,
            request_id=_next(ids),
            owner="Marco",
            principal="admin",
            arguments={"task_id": str(task_id), "confirmed": True},
        )
    )


def test_archive_supersedes_pending_undispatched_projection(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    event_id = seed_events(factory, ids, context, task_id)[0]
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        archived = _archive(_port(session, ids), ids, task_id, admin_run)
        assert archived.ok, archived
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "superseded"


def test_archive_supersedes_pending_and_claimed_undispatched_projection_and_stales_worker(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    pending_event, claimed_event = seed_events(factory, ids, context, task_id, count=2)
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        service = ProjectionService(session, uuid_factory=lambda: _next(ids))
        first_claim = service.claim_next(worker_id="projection-worker", now=NOW, ttl=timedelta(minutes=5))
        assert first_claim is not None
        assert first_claim.event_id == pending_event
        first_attempt = service.begin_attempt(
            event_id=first_claim.event_id,
            claim_token=first_claim.claim_token,
            claim_revision=first_claim.claim_revision,
            worker_id="projection-worker",
            request_identity="first-applied",
            request_payload={"content_version_id": "v2"},
            intended_external_id=None,
            started_at=NOW,
        )
        service.record_observation_and_adjudicate(
            attempt_id=first_attempt.attempt_id,
            observation_kind="reread",
            observed_applied=True,
            observed_identity=first_attempt.request_sha256,
            reread_complete=True,
            evidence=external_evidence(observed_identity=first_attempt.request_sha256),
            decided_by="automatic",
            decision_reason="independent reread confirmed application",
            observed_at=NOW + timedelta(seconds=1),
            claim_token=first_claim.claim_token,
            claim_revision=first_claim.claim_revision,
            worker_id="projection-worker",
        )
        claim = service.claim_next(
            worker_id="projection-worker", now=NOW + timedelta(seconds=2), ttl=timedelta(minutes=5)
        )
        assert claim is not None
        assert claim.event_id == claimed_event

        archived = _archive(_port(session, ids), ids, task_id, admin_run)
        assert archived.ok, archived
        assert session.get(tx.ProjectionOutboxEvent, claimed_event).state == "superseded"
        with pytest.raises(TransitionAuthorityError):
            service.begin_attempt(
                event_id=claim.event_id,
                claim_token=claim.claim_token,
                claim_revision=claim.claim_revision,
                worker_id="projection-worker",
                request_identity="stale-worker",
                request_payload={"content_version_id": "v3"},
                intended_external_id=None,
                started_at=NOW + timedelta(seconds=3),
            )


def test_archive_refuses_dispatched_projection_without_partial_archive(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    event_id = seed_events(factory, ids, context, task_id)[0]
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        service = ProjectionService(session, uuid_factory=lambda: _next(ids))
        claim = service.claim_next(worker_id="projection-worker", now=NOW, ttl=timedelta(minutes=5))
        assert claim is not None
        attempt = service.begin_attempt(
            event_id=event_id,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="projection-worker",
            request_identity="dispatch-in-flight",
            request_payload={"content_version_id": "v2"},
            intended_external_id=None,
            started_at=NOW,
        )

        blocked = _archive(_port(session, ids), ids, task_id, admin_run)
        assert blocked.ok is False
        assert blocked.code == "TASK_NOT_RESTING"
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert state.archived_at is None
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "claimed"
        assert session.get(tx.ProjectionAttempt, attempt.attempt_id).state == "dispatched"
        assert session.scalar(
            select(func.count()).select_from(models.DishMutationReceipt).where(
                models.DishMutationReceipt.generation_id == context["generation_id"],
                models.DishMutationReceipt.task_id == task_id,
                models.DishMutationReceipt.archive_changed.is_(True),
            )
        ) == 0


@pytest.mark.parametrize("unsafe_state", ["uncertain", "blocked"])
def test_archive_refuses_unresolved_projection_event_states_without_partial_archive(
    workflow_db, unsafe_state
) -> None:
    factory, ids, context, task_id = workflow_db
    event_id = seed_events(factory, ids, context, task_id)[0]
    admin_run = _next(ids)
    with session_scope(factory) as session:
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        event.state = unsafe_state
        event.claim_owner = None
        event.claim_token = None
        event.claim_expires_at = None
        event.outbox_revision += 1
        event.terminal_at = NOW
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        blocked = _archive(_port(session, ids), ids, task_id, admin_run)
        assert blocked.ok is False
        assert blocked.code == "TASK_NOT_RESTING"
        assert session.get(models.DishState, (context["generation_id"], task_id)).archived_at is None
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == unsafe_state


def test_archive_accepts_proven_not_applied_projection(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    event_id = seed_events(factory, ids, context, task_id)[0]
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        service = ProjectionService(session, uuid_factory=lambda: _next(ids))
        claim = service.claim_next(worker_id="projection-worker", now=NOW, ttl=timedelta(minutes=5))
        assert claim is not None
        attempt = service.begin_attempt(
            event_id=event_id,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="projection-worker",
            request_identity="not-applied",
            request_payload={"content_version_id": "v2"},
            intended_external_id=None,
            started_at=NOW,
        )
        adjudication = service.record_observation_and_adjudicate(
            attempt_id=attempt.attempt_id,
            observation_kind="reread",
            observed_applied=False,
            observed_identity=None,
            reread_complete=True,
            evidence=external_evidence(absent=True),
            decided_by="automatic",
            decision_reason="independent reread proves non-application",
            observed_at=NOW + timedelta(seconds=1),
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="projection-worker",
        )
        assert adjudication.outcome == "not_applied"
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "pending"

        archived = _archive(_port(session, ids), ids, task_id, admin_run)
        assert archived.ok, archived
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "superseded"
        assert session.get(tx.ProjectionAttempt, attempt.attempt_id).state == "not_applied"


def test_post_burn_projection_rows_are_forensic_and_not_rewritten(workflow_db, monkeypatch) -> None:
    factory, ids, context, task_id = workflow_db
    event_id = seed_events(factory, ids, context, task_id)[0]
    with session_scope(factory) as session:
        monkeypatch.setattr(transition_module, "external_projection_required", lambda *_args, **_kwargs: False)
        service = ProjectionService(session, uuid_factory=lambda: _next(ids))
        count = service.terminalize_task_projection_for_archive(
            generation_id=context["generation_id"], task_id=task_id, at=NOW
        )
        assert count == 0
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "pending"
