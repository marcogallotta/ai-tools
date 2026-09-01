"""Authority proof over the SQLite-backed PostgreSQL command/workflow fixture."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import stage3_models as wf
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.protocol import AuthenticationError, PostgresProtocolService, ScopedBearerAuthenticator
from dish_pg.transition import ProjectionService
from dish_pg.workflow import WorkflowAuthorityError, WorkflowAuthorityService
from tests.support.postgresql.workflow import NOW, workflow_db

OVERRIDE_REASON = "Bounded proactive planning was explicitly selected"


def _call(command, run_id, request_id, arguments, *, principal="agent", owner="owner-1"):
    return CommandCall(command, arguments, owner, principal, run_id, request_id, NOW)


def _port(session, ids, context, agent_run, admin_run):
    workflow = WorkflowAuthorityService(session, uuid_factory=lambda: next(ids))
    for run_id, owner, agent in (
        (agent_run, "owner-1", "claude"),
        (admin_run, "marco", "gpt"),
    ):
        workflow.register_run(
            run_id=run_id,
            generation_id=context["generation_id"],
            owner_id=owner,
            agent=agent,
            capability_digest=run_id.bytes * 2,
            registered_at=NOW,
        )
    ProjectionService(session, uuid_factory=lambda: next(ids)).activate_epoch(
        generation_id=context["generation_id"],
        activation_reason="Planning authority boundary test",
        created_at=NOW,
        external_effects_enabled=True,
    )
    return PostgresCommandPort(
        session,
        cursor_secret=b"planning-authority-test-secret!!",
        uuid_factory=lambda: next(ids),
        lease_duration=timedelta(minutes=10),
    )


def _consume_override(port, ids, task_id, run_id):
    issued_request = next(ids)
    issued = port.execute(
        _call(
            "start",
            run_id,
            issued_request,
            {"task_id": str(task_id), "kind": "planning", "agent": "claude"},
        )
    )
    assert issued.code == "CONFIRMATION_REQUIRED", issued
    consumed_request = next(ids)
    consumed = port.execute(
        _call(
            "start",
            run_id,
            consumed_request,
            {
                "task_id": str(task_id),
                "kind": "planning",
                "agent": "claude",
                "intent_challenge_id": issued.data["intent_challenge_id"],
                "intent_basis": "agent_override",
                "override_reason": OVERRIDE_REASON,
            },
        )
    )
    assert consumed.ok, consumed
    return issued_request, consumed_request, issued, consumed


def _assert_no_mutation_authority(session):
    models = (
        wf.MarcoAuthorizationGrant,
        wf.MarcoAuthorizationState,
        wf.MarcoAuthorizationEvent,
        wf.HumanReviewRequirement,
        wf.HumanReviewDecision,
    )
    assert [session.scalar(select(func.count()).select_from(model)) for model in models] == [0] * 5


def test_archived_task_rejects_planning_challenge_without_durable_challenge(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    agent_run, admin_run = next(ids), next(ids)
    with session_scope(factory) as session:
        port = _port(session, ids, context, agent_run, admin_run)
        archived = port.execute(
            _call(
                "archive",
                admin_run,
                next(ids),
                {"task_id": str(task_id), "confirmed": True},
                principal="admin",
                owner="marco",
            )
        )
        assert archived.ok, archived

        attempted = port.execute(
            _call(
                "start",
                agent_run,
                next(ids),
                {"task_id": str(task_id), "kind": "planning", "agent": "claude"},
            )
        )

        assert attempted.ok is False
        assert attempted.code == "TASK_ARCHIVED"
        assert (
            session.scalar(
                select(func.count()).select_from(wf.PlanningIntentChallenge)
            )
            == 0
        )


def test_override_cannot_be_authorization_or_lease_fence(workflow_db):
    factory, ids, context, task_id = workflow_db
    agent_run, admin_run = next(ids), next(ids)
    with session_scope(factory) as session:
        port = _port(session, ids, context, agent_run, admin_run)
        issued_request, consumed_request, issued, consumed = _consume_override(
            port, ids, task_id, agent_run
        )
        challenge_id = uuid.UUID(issued.data["intent_challenge_id"])
        operation_id = uuid.UUID(consumed.data["operation_id"])
        lease_id = uuid.UUID(consumed.data["lease_id"])
        challenge = session.get(wf.PlanningIntentChallenge, challenge_id)
        assert (
            challenge.state,
            challenge.claiming_request_id,
            challenge.intent_basis,
            challenge.override_reason,
        ) == ("consumed", consumed_request, "agent_override", OVERRIDE_REASON)
        _assert_no_mutation_authority(session)
        principals = dict(
            session.execute(
                select(wf.ServiceRequest.request_id, wf.ServiceRequest.principal_class).where(
                    wf.ServiceRequest.request_id.in_((issued_request, consumed_request))
                )
            ).all()
        )
        assert principals == {issued_request: "agent", consumed_request: "agent"}

        blocked = port.execute(
            _call(
                "authorize-governed-change",
                agent_run,
                next(ids),
                {
                    "task_id": str(task_id),
                    "operation_id": str(operation_id),
                    "field_name": "Locks",
                    "before": "a",
                    "after": "b",
                    "reason": "exact authorization is still required",
                },
            )
        )
        assert blocked.code == "PRINCIPAL_SCOPE_MISMATCH"
        execution_id = session.scalar(
            select(wf.CommandExecution.execution_id).where(
                wf.CommandExecution.request_id == consumed_request
            )
        )
        with pytest.raises(WorkflowAuthorityError, match="unknown authorization"):
            port.workflow.reserve_marco_authorization(
                grant_id=challenge_id,
                reservation_token=next(ids),
                execution_id=execution_id,
                reserved_at=NOW,
            )
        wrong_fence = port.execute(
            _call(
                "recover-lease",
                admin_run,
                next(ids),
                {
                    "task_id": str(task_id),
                    "operation_id": str(operation_id),
                    "lease_id": str(challenge_id),
                },
                principal="admin",
                owner="marco",
            )
        )
        assert wrong_fence.code == "EXACT_LEASE_REQUIRED"
        assert session.get(wf.ServiceLease, lease_id).state == "active"
        _assert_no_mutation_authority(session)


def test_override_does_not_elevate_action_route_scope(workflow_db):
    factory, ids, context, task_id = workflow_db
    agent_run, admin_run = next(ids), next(ids)
    with session_scope(factory) as session:
        port = _port(session, ids, context, agent_run, admin_run)
        protocol = PostgresProtocolService(
            port,
            ScopedBearerAuthenticator(
                action_token="action-secret", private_token="private-secret"
            ),
        )

        def assert_action_scope_unchanged():
            with pytest.raises(AuthenticationError, match="not exposed"):
                protocol.handle(
                    command_name="authorize-governed-change",
                    authorization="Bearer action-secret",
                    body_loader=lambda: {},
                    owner_id="owner-1",
                    now=NOW,
                    route_scope="action",
                )

        assert_action_scope_unchanged()
        _consume_override(port, ids, task_id, agent_run)
        assert_action_scope_unchanged()
