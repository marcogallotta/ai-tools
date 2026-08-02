from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import ACTION_COMMANDS, RETAINED_COMMANDS
from dish_pg.command_port import (
    PORTED_MUTATION_COMMANDS,
    CommandCall,
    PostgresCommandPort,
)
from dish_pg.database import session_scope
from dish_pg.openapi import postgres_action_openapi
from dish_pg.planner import (
    AuthorityFence,
    AuthoritativeSnapshot,
    CanonicalCommandIntent,
    EffectObservation,
    adjudicate_effect,
    plan_command,
)
from dish_pg.protocol import AuthenticationError, PostgresProtocolService, ScopedBearerAuthenticator
from dish_pg.read_model import InvalidCursor
from dish_pg.transition import ProjectionService
from dish_tool.workflow_policy import WorkflowSnapshot
from tests.postgresql.test_stage2_core_authority import _import_one
from tests.postgresql.test_stage3_workflow_authority import NOW, _next, _register_run, workflow_db

SECRET = b"stage-4-cursor-secret-32-bytes!!"
ROOT = Path(__file__).resolve().parents[2]


def _port(session, ids) -> PostgresCommandPort:
    generation_id = session.scalar(
        select(models.AuthorityGeneration.generation_id).where(
            models.AuthorityGeneration.status == "active"
        )
    )
    ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
        generation_id=generation_id,
        activation_reason="Stage 4 command-port test authority",
        created_at=NOW,
    )
    return PostgresCommandPort(
        session,
        cursor_secret=SECRET,
        uuid_factory=lambda: _next(ids),
        lease_duration=timedelta(minutes=10),
    )


def _call(command, *, run_id, request_id=None, arguments=None, principal="agent", owner="owner-1"):
    return CommandCall(
        command_name=command,
        arguments=arguments or {},
        owner_id=owner,
        principal_class=principal,
        run_id=run_id,
        request_id=request_id,
        now=NOW,
    )


def test_stage4_ports_every_retained_mutation_and_action_path() -> None:
    queries = {"sections", "section-tasks", "read"}
    assert PORTED_MUTATION_COMMANDS == set(RETAINED_COMMANDS) - queries
    document = postgres_action_openapi()
    assert set(document["paths"]) == {f"/v1/action/{name}" for name in ACTION_COMMANDS}
    assert document["paths"]["/v1/action/inspect"]["post"]["x-openai-isConsequential"] is True
    checked_in = json.loads((ROOT / "openapi/dish-postgresql-action.openapi.json").read_text())
    assert checked_in == document


def test_planner_delegates_legality_and_adjudicates_exact_effects() -> None:
    workflow = WorkflowSnapshot(
        operation_status="open",
        operation_phase="prepare_required",
        persisted_actions=("prepare",),
        live_status="pending-research",
        live_section_gid="rq",
        verification_queue_gid="vq",
        cycle_reviewed=False,
        latest_cycle_outcome=None,
        latest_cycle_route=None,
        validation_rules=(),
        operation_kind="initial",
    )
    snapshot = AuthoritativeSnapshot(
        generation_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        fence=AuthorityFence(1, 1, 1, 1, 1, "prepare_required"),
        workflow=workflow,
        task_exists=True,
    )
    plan = plan_command(
        snapshot=snapshot,
        intent=CanonicalCommandIntent("prepare", {}, "agent", "owner", str(uuid.uuid4())),
        pinned_now=NOW,
    )
    assert plan.legal is True
    assert [mutation.kind for mutation in plan.mutations] == [
        "activate_content_version",
        "place_verification_queue",
        "append_operation_step",
        "open_verification_cycle",
        "advance_operation",
    ]
    confirmed = adjudicate_effect(
        intended_identity="intent-1",
        observation=EffectObservation("intent-1", "intent-1", True, True, {"reread": 1}),
    )
    uncertain = adjudicate_effect(
        intended_identity="intent-1",
        observation=EffectObservation("intent-1", None, None, False, {}),
    )
    assert (confirmed.outcome, confirmed.retry_safe) == ("confirmed", False)
    assert (uncertain.outcome, uncertain.retry_safe) == ("uncertain", False)


def test_authoritative_sections_and_registry_bound_pagination(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        _import_one(session, ids, context, asana_gid="123456790")
        _import_one(session, ids, context, asana_gid="123456791")
        port = _port(session, ids)
        sections = port.execute(_call("sections", run_id=_next(ids)))
        assert sections.ok and sections.data["sections"][0]["workflow_role"] == "research_queue"
        first = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={"section_gid": "1217084805070731", "page_size": 2},
            )
        )
        assert len(first.data["tasks"]) == 2
        assert first.data["next_cursor"]
        second = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={
                    "section_gid": "1217084805070731",
                    "page_size": 2,
                    "cursor": first.data["next_cursor"],
                },
            )
        )
        assert len(second.data["tasks"]) == 1
        with pytest.raises(InvalidCursor):
            port.execute(
                _call(
                    "section-tasks",
                    run_id=_next(ids),
                    arguments={
                        "section_gid": "1217084805070731",
                        "page_size": 3,
                        "cursor": first.data["next_cursor"],
                    },
                )
            )


def test_create_commits_one_authoritative_bundle_and_exact_replay(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        call = _call(
            "create",
            run_id=run_id,
            request_id=request_id,
            arguments={"title": "New governed dish"},
        )
        first = port.execute(call)
        replay = port.execute(call)
        created = session.scalar(
            select(models.DishTask).where(models.DishTask.task_id == uuid.UUID(first.data["task_id"]))
        )
        assert first.ok and created.creation_route == "create"
        assert replay.ok and replay.request_replayed is True
        assert session.scalar(
            select(func.count()).select_from(models.DishTask).where(models.DishTask.creation_route == "create")
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        ) == 1


def test_planning_challenge_then_fresh_start_opens_exact_operation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        first = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "kind": "planning", "agent": "claude"},
            )
        )
        assert first.code == "CONFIRMATION_REQUIRED"
        assert session.scalar(select(func.count()).select_from(wf.CommandExecution)) == 0
        second = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "planning",
                    "agent": "claude",
                    "intent_challenge_id": first.data["intent_challenge_id"],
                    "intent_basis": "user_requested",
                },
            )
        )
        operation = session.get(wf.WorkflowOperation, uuid.UUID(second.data["operation_id"]))
        lease = session.get(wf.ServiceLease, uuid.UUID(second.data["lease_id"]))
        assert second.ok and operation.kind == "planning"
        assert lease.operation_id == operation.operation_id and lease.state == "active"
        challenge = session.get(
            wf.PlanningIntentChallenge, uuid.UUID(first.data["intent_challenge_id"])
        )
        assert challenge.state == "consumed"


def test_protocol_authenticates_before_loading_body(workflow_db) -> None:
    factory, ids, _context, _task_id = workflow_db
    loaded = False

    def body_loader():
        nonlocal loaded
        loaded = True
        return {"client": {"run_id": str(_next(ids))}, "arguments": {}}

    with session_scope(factory) as session:
        service = PostgresProtocolService(
            _port(session, ids),
            ScopedBearerAuthenticator(action_token="action-secret", private_token="private-secret"),
        )
        with pytest.raises(AuthenticationError):
            service.handle(
                command_name="sections",
                authorization="Bearer wrong",
                body_loader=body_loader,
                owner_id="owner-1",
                now=NOW,
            )
        assert loaded is False
