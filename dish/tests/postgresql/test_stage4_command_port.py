from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import ACTION_COMMANDS
from dish_pg.command_port import CommandCall, PostgresCommandPort, _task_reference_from_dish
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
from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions
from tests.support.canonical import TASK
from tests.support.postgresql.core import _import_one
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db

SECRET = b"stage-4-cursor-secret-32-bytes!!"
ROOT = Path(__file__).resolve().parents[2]


def test_stage4_postgresql_action_path_contract() -> None:
    document = postgres_action_openapi()
    assert set(document["paths"]) == {f"/v1/action/{name}" for name in ACTION_COMMANDS}
    assert document["paths"]["/v1/action/inspect"]["post"]["x-openai-isConsequential"] is True
    checked_in = json.loads((ROOT / "openapi/dish-postgresql-action.openapi.json").read_text())
    assert checked_in == document


@pytest.mark.parametrize(
    ("dish_value", "expected"),
    [
        ("123456789", "123456789"),
        (
            "https://app.asana.com/1/1200569426771227/project/1217084805070730/task/123456789",
            "123456789",
        ),
        ("e55e1667-2a0a-545a-a191-091738d9c347", "e55e1667-2a0a-545a-a191-091738d9c347"),
        ("", None),
        # malformed/unsupported shapes must not accidentally resolve to a task
        ("https://app.asana.com/1/bad/url/shape", None),
        ("/dishes/not-a-uuid/some-slug", None),
        ("https://example.com/nonsense", None),
        # an arbitrary string is passed through unchanged rather than dropped,
        # so resolve_task's own not-found handling still applies to it
        ("totally-unrelated-string", "totally-unrelated-string"),
    ],
)
def test_task_reference_from_dish_reduces_known_shapes(dish_value, expected) -> None:
    assert _task_reference_from_dish(dish_value) == expected


def test_prepare_stamps_researched_by_and_self_verified_from_agent(workflow_db) -> None:
    """PG must own tool-owned process fields on initial prepare, like legacy.

    Reproduces the exact PROD shape: a candidate with non-canonical actor-line
    grammar *and* "Verification protocol release: None" (both present in the
    real failing gpt submission). Legacy's initial ``prepare`` deterministically
    overwrites "Status detail", "Resume status", "Verification protocol
    release", "Verified by", "Researched by", and "Self-verified" from
    known/computed values regardless of what the candidate wrote there; PG
    must do the same rather than validating the caller's self-reported text
    as-is.
    """
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        author_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run, agent="gpt")
        malformed_candidate = TASK.replace(
            "Researched by: ChatGPT — GPT-5, 2026-07-25",
            "Researched by: gpt — gpt-5.6-sol, 2026-08-12",
        ).replace(
            "Self-verified: ChatGPT — GPT-5, 2026-07-25",
            "Self-verified: gpt — gpt-5.6-sol, 2026-08-12",
        ).replace(
            "Verification protocol release: abc123",
            "Verification protocol release: None",
        )
        result = port.execute(
            _call(
                "prepare",
                run_id=author_run,
                request_id=_next(ids),
                owner="owner-1",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "file_text": malformed_candidate,
                    "agent": "gpt",
                    "model": "gpt-5.6-sol",
                },
            )
        )
        assert result.ok, (result.code, result.http_status, result.data)

        version = session.get(
            models.ContentVersion, uuid.UUID(result.data["content_version_id"])
        )
        assert "Verification protocol release: None" not in version.body
        assert "Researched by: gpt — gpt-5.6-sol, 2026-08-12" not in version.body
        assert "Self-verified: gpt — gpt-5.6-sol, 2026-08-12" not in version.body
        assert "Verified by: None" in version.body
        assert "Status detail: None" in version.body
        assert "Resume status: None" in version.body


def test_inspect_resolves_task_from_dish_argument(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        author_run = _next(ids)
        verifier_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="codex",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        _prepare_for_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"], run_id=author_run
        )
        _start_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"], run_id=verifier_run
        )
        result = port.execute(
            _call(
                "inspect",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "dish": "123456789",
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "independence_attestation": "I independently inspected this exact candidate.",
                },
            )
        )
    assert result.ok, (result.code, result.http_status, result.data)


def test_inspect_without_task_reference_still_requires_one(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "inspect",
                run_id=run_id,
                request_id=_next(ids),
                owner="owner-1",
                principal="verification",
                arguments={"agent": "codex"},
            )
        )
    assert not result.ok
    assert result.code == "TASK_REQUIRED"


def test_inspect_with_unresolvable_dish_argument_does_not_accidentally_resolve(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "inspect",
                run_id=run_id,
                request_id=_next(ids),
                owner="owner-1",
                principal="verification",
                arguments={"dish": "https://example.com/nonsense", "agent": "codex"},
            )
        )
    assert not result.ok
    assert result.code == "TASK_REQUIRED"


@pytest.mark.parametrize(
    ("command", "principal"),
    [
        ("prepare", "agent"),
        ("reject", "verification"),
        ("approve", "verification"),
        ("submit", "agent"),
    ],
)
def test_postgresql_planner_matches_shared_legal_actions(command: str, principal: str) -> None:
    workflow = WorkflowSnapshot(
        operation_status="open",
        operation_phase="prepare_required",
        persisted_actions=("prepare", "reject"),
        live_status="pending-research",
        live_section_gid="rq",
        verification_queue_gid="vq",
        verifier_established=False,
        latest_cycle_outcome=None,
        latest_cycle_route=None,
        validation_rules=(),
        operation_kind="initial",
    )
    allowed = legal_actions(workflow)
    snapshot = AuthoritativeSnapshot(
        generation_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        fence=AuthorityFence(1, 1, 1, 1, 1, "prepare_required"),
        workflow=workflow,
        task_exists=True,
    )

    plan = plan_command(
        snapshot=snapshot,
        intent=CanonicalCommandIntent(command, {}, principal, "owner", str(uuid.uuid4())),
        pinned_now=NOW,
    )

    assert plan.legal is (command in allowed)
    if not plan.legal:
        assert plan.result_code == "ACTION_NOT_LEGAL"
        assert plan.recovery_guidance["allowed_actions"] == tuple(allowed)


def test_planner_delegates_legality_and_adjudicates_exact_effects() -> None:
    workflow = WorkflowSnapshot(
        operation_status="open",
        operation_phase="prepare_required",
        persisted_actions=("prepare",),
        live_status="pending-research",
        live_section_gid="rq",
        verification_queue_gid="vq",
        verifier_established=False,
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
        observation=EffectObservation("intent-1", "intent-1", True, True, True, {"reread": 1}),
    )
    uncertain = adjudicate_effect(
        intended_identity="intent-1",
        observation=EffectObservation("intent-1", None, None, False, False, {}),
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


def test_canonical_section_id_lists_tasks_without_section_gid(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port = _port(session, ids)
        result = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={"section_id": str(context["section_id"])},
            )
        )

    assert result.ok
    assert [item["dish_id"] for item in result.data["tasks"]] == [str(task_id)]
    assert result.data["tasks"][0]["section_id"] == str(context["section_id"])
    assert result.data["tasks"][0]["task_gid"] == "123456789"


def test_read_resolves_canonical_dish_id_without_task_gid(workflow_db) -> None:
    factory, ids, _context, task_id = workflow_db
    with session_scope(factory) as session:
        port = _port(session, ids)
        result = port.execute(
            _call("read", run_id=_next(ids), arguments={"dish_id": str(task_id)})
        )

    assert result.ok
    assert result.data["dish_id"] == str(task_id)
    assert result.data["identity_binding"] == {
        "dish_id": str(task_id),
        "task_gid": "123456789",
    }


def test_unknown_canonical_read_and_section_ids_fail_locally(workflow_db) -> None:
    factory, ids, _context, _task_id = workflow_db
    unknown_dish = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    unknown_section = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    with session_scope(factory) as session:
        port = _port(session, ids)
        read_result = port.execute(
            _call("read", run_id=_next(ids), arguments={"dish_id": str(unknown_dish)})
        )
        section_result = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={"section_id": str(unknown_section)},
            )
        )

    assert read_result.ok is False
    assert (read_result.code, read_result.http_status) == ("DISH_NOT_FOUND", 404)
    assert read_result.data == {"dish_reference": str(unknown_dish)}
    assert section_result.ok is False
    assert (section_result.code, section_result.http_status) == ("SECTION_NOT_FOUND", 404)
    assert section_result.data == {"section_reference": str(unknown_section)}


def test_start_resolves_canonical_dish_id_without_task_gid(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"dish_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )

    assert result.ok, (result.code, result.http_status, result.data)
    assert result.data["operation_id"]


def test_legacy_task_gid_alias_resolves_locally_for_start(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"task_gid": "123456789", "kind": "initial", "agent": "claude"},
            )
        )

    assert result.ok, (result.code, result.http_status, result.data)


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
            select(models.DishTask).where(models.DishTask.task_id == uuid.UUID(first.data["dish_id"]))
        )
        assert first.ok and created.creation_route == "create"
        assert first.data["dish_id"] == first.data["task_id"]
        assert first.data["section_id"] == str(context["section_id"])
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
