from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import ADMIN_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.database import session_scope
from dish_pg.document_authority import parse_canonical_document
from dish_pg.workflow import WorkflowAuthorityError
from tests.support.canonical import TASK
from tests.support.postgresql.command import (
    _add_destination_section,
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
    _verification_ready,
)
from tests.support.postgresql.workflow import _next, _register_run, workflow_db


def test_invalid_canonical_prepare_failure_is_immutable_and_replays(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    request_id = _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        call = _call(
            "prepare",
            run_id=run_id,
            request_id=request_id,
            arguments={
                "task_id": str(task_id),
                "operation_id": started.data["operation_id"],
                "file_text": "<script>alert(1)</script>\n---\nStatus: pending-verification\n",
            },
        )
        first = port.execute(call)
        second = port.execute(call)
        assert first.code == "VALIDATION_FAILED"
        assert second.code == first.code
        assert second.data == first.data
        assert second.request_replayed is True
        operation = session.get(
            wf.WorkflowOperation, uuid.UUID(started.data["operation_id"])
        )
        assert operation.phase == "prepare_required"
        assert session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        ) is not None


def test_operation_target_is_required_and_failure_replays(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    request_id = _next(ids)
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        _start_initial(port, ids, task_id=task_id, run_id=run_id)
        call = _call(
            "prepare",
            run_id=run_id,
            request_id=request_id,
            arguments={"task_id": str(task_id), "file_text": TASK},
        )
        first = port.execute(call)
        second = port.execute(call)
        assert first.code == "OPERATION_ID_REQUIRED"
        assert second.code == "OPERATION_ID_REQUIRED"
        assert second.request_replayed is True


def test_verification_ordinary_start_resolves_unique_open_operation_and_requires_start_occurrence(workflow_db) -> None:
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
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
        )
        bypass = port.execute(
            _call(
                "inspect",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "independence_attestation": "I independently inspected this exact candidate.",
                },
            )
        )
        one_sided_target = port.execute(
            _call(
                "start",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                arguments={
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "codex",
                    "independence_attestation": "I independently inspected this exact candidate.",
                    "target_operation_id": started.data["operation_id"],
                },
            )
        )
        ordinary = port.execute(
            _call(
                "start",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                arguments={
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "codex",
                    "independence_attestation": "I independently inspected this exact candidate.",
                },
            )
        )
        assert bypass.code == "VERIFICATION_START_REQUIRED"
        assert one_sided_target.code == "VERIFICATION_TARGET_PAIR_REQUIRED"
        assert ordinary.ok
        assert ordinary.data["operation_id"] == started.data["operation_id"]
        assert _inspect(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        ).ok


def test_planning_confirmation_is_bound_to_registered_agent_and_exact_target(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            agent="claude",
        )
        port = _port(session, ids)
        initial = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "planning",
                    "agent": "claude",
                },
            )
        )
        assert initial.code == "CONFIRMATION_REQUIRED"
        challenge_id = initial.data["intent_challenge_id"]
        changed_agent = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "planning",
                    "agent": "gpt",
                    "intent_challenge_id": challenge_id,
                    "intent_basis": "user_requested",
                },
            )
        )
        changed_target = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "planning",
                    "agent": "claude",
                    "unexpected_target_field": "changed",
                    "intent_challenge_id": challenge_id,
                    "intent_basis": "user_requested",
                },
            )
        )
        exact = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "planning",
                    "agent": "claude",
                    "intent_challenge_id": challenge_id,
                    "intent_basis": "user_requested",
                },
            )
        )
        assert changed_agent.code == "PLANNING_AGENT_MISMATCH"
        assert changed_target.code == "PLANNING_CHALLENGE_MISMATCH"
        assert exact.ok
        operation = session.get(
            wf.WorkflowOperation, uuid.UUID(exact.data["operation_id"])
        )
        assert operation is not None
        actor = session.scalar(
            select(wf.OperationActorFact).where(
                wf.OperationActorFact.operation_id == operation.operation_id
            )
        )
        assert actor.agent == "claude"


def test_malformed_operation_target_failure_is_immutable(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    request_id = _next(ids)
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        call = _call(
            "prepare",
            run_id=run_id,
            request_id=request_id,
            arguments={
                "task_id": str(task_id),
                "operation_id": "not-a-uuid",
                "file_text": TASK,
            },
        )
        first = port.execute(call)
        second = port.execute(call)
        assert first.code == "INVALID_OPERATION_ID"
        assert first.http_status == 400
        assert second.code == first.code
        assert second.data == first.data
        assert second.request_replayed is True
        assert session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        ) is not None


def test_postgresql_command_inventory_matches_independent_stage_a_baseline() -> None:
    baseline = json.loads(
        (Path(__file__).parents[2] / "docs" / "database-backend-stage-a-baseline.json").read_text()
    )
    expected = set(baseline["target_treatments"])
    assert expected | {"revise-section-registry", "hold-reject"} == set(COMMAND_DEFINITIONS)
    assert "holds" in ADMIN_COMMANDS
    assert "resolved" in ADMIN_COMMANDS
    assert "planning-intent-settlement" in ADMIN_COMMANDS
    assert "settle-planning-intent" not in COMMAND_DEFINITIONS
