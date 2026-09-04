from __future__ import annotations

import uuid

from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from tests.support.postgresql.command import _call, _port, _start_initial
from tests.support.postgresql.workflow import _next, _register_run, workflow_db


def test_revoked_run_cannot_reenter_after_simulated_unarchive_but_cook_log_stays_legal(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    old_run = _next(ids)
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=old_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        port = _port(session, ids)
        archived = port.execute(
            _call(
                "archive",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id), "confirmed": True},
            )
        )
        assert archived.ok, archived
        assert session.scalar(
            select(func.count()).select_from(wf.TaskRunRevocation).where(
                wf.TaskRunRevocation.generation_id == context["generation_id"],
                wf.TaskRunRevocation.task_id == task_id,
                wf.TaskRunRevocation.run_id == old_run,
            )
        ) == 1

        # Simulate the state a future explicit unarchive would expose without
        # implementing or specifying an unarchive command in this task.
        state = session.get(models.DishState, (context["generation_id"], task_id))
        state.archived_at = None
        session.flush()

        before_challenges = session.scalar(
            select(func.count()).select_from(wf.PlanningIntentChallenge)
        )
        stale = port.execute(
            _call(
                "start",
                run_id=old_run,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "kind": "planning", "agent": "claude"},
            )
        )
        assert stale.ok is False
        assert stale.code == "AUTHORITY_MISMATCH"
        assert session.scalar(
            select(func.count()).select_from(wf.PlanningIntentChallenge)
        ) == before_challenges

        cook_log = port.execute(
            _call(
                "record-cook-log",
                run_id=old_run,
                request_id=_next(ids),
                arguments={
                    "dish_id": str(task_id),
                    "agent": "claude",
                    "text": "lifecycle-neutral history remains writable after simulated unarchive",
                },
            )
        )
        assert cook_log.ok, cook_log

        fresh_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=fresh_run,
        )
        fresh = port.execute(
            _call(
                "start",
                run_id=fresh_run,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "kind": "planning", "agent": "claude"},
            )
        )
        assert fresh.ok is False
        assert fresh.code == "CONFIRMATION_REQUIRED"


def test_archive_terminalizes_active_abandonment_and_open_operation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        operation_id = uuid.UUID(started.data["operation_id"])
        operation = session.get(wf.WorkflowOperation, operation_id)
        operation.phase = "held_evidence"
        session.flush()
        abandonment = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": str(operation_id),
                    "lease_id": started.data["lease_id"],
                    "reason": "archive must own terminal cleanup",
                },
            )
        )
        assert abandonment.ok, abandonment
        assert abandonment.data["state"] == "blocked"
        abandonment_id = uuid.UUID(abandonment.data["abandonment_id"])

        archived = port.execute(
            _call(
                "archive",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id), "confirmed": True},
            )
        )
        assert archived.ok, archived
        attempt = session.get(wf.AbandonmentAttempt, abandonment_id)
        assert attempt.state == "cancelled"
        assert attempt.terminal_at is not None
        assert session.get(wf.WorkflowOperation, operation_id).lifecycle == "abandoned"
        assert session.scalar(
            select(func.count()).select_from(wf.WorkflowOperation).where(
                wf.WorkflowOperation.generation_id == context["generation_id"],
                wf.WorkflowOperation.task_id == task_id,
                wf.WorkflowOperation.lifecycle == "open",
            )
        ) == 0


def test_archive_settles_claimed_planning_challenge_without_erasing_claim_provenance(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    agent_run = _next(ids)
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=agent_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        port = _port(session, ids)
        issued = port.execute(
            _call(
                "start",
                run_id=agent_run,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "kind": "planning", "agent": "claude"},
            )
        )
        assert issued.code == "CONFIRMATION_REQUIRED"
        challenge = session.get(
            wf.PlanningIntentChallenge, uuid.UUID(issued.data["intent_challenge_id"])
        )
        challenge.state = "claimed"
        challenge.claiming_request_id = challenge.issuing_request_id
        challenge.intent_basis = "agent_override"
        challenge.override_reason = "historical claim provenance"
        session.flush()

        claimed_request_id = challenge.claiming_request_id
        intent_basis = challenge.intent_basis
        override_reason = challenge.override_reason
        archived = port.execute(
            _call(
                "archive",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id), "confirmed": True},
            )
        )
        assert archived.ok, archived
        session.refresh(challenge)
        assert challenge.state == "settled"
        assert challenge.settlement_reason == "Dish archived"
        assert challenge.claiming_request_id == claimed_request_id
        assert challenge.intent_basis == intent_basis
        assert challenge.override_reason == override_reason
