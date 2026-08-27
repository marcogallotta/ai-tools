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


def _reject_large_round(
    port, ids, *, task_id, operation_id, run_id, owner, agent, round_number
):
    candidate = TASK.replace(
        "A compact side dish for testing texture.",
        f"A compact side dish for testing texture, correction round {round_number}.",
    )
    return port.execute(
        _call(
            "reject",
            run_id=run_id,
            request_id=_next(ids),
            owner=owner,
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": operation_id,
                "agent": agent,
                "route": "large",
                "reason": f"round {round_number} still needs material correction",
                "file_text": candidate,
            },
        )
    )


def _open_verifier_round(
    session, port, ids, context, *, task_id, operation_id, owner, agent
):
    run_id = _next(ids)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=run_id,
        owner=owner,
        agent=agent,
    )
    _start_verification(
        port,
        ids,
        task_id=task_id,
        operation_id=operation_id,
        run_id=run_id,
        owner=owner,
        agent=agent,
    )
    _inspect(
        port,
        ids,
        task_id=task_id,
        operation_id=operation_id,
        run_id=run_id,
        owner=owner,
        agent=agent,
    )
    return run_id


def _active_content(session, context, task_id):
    state = session.get(models.DishState, (context["generation_id"], task_id))
    return session.get(models.ContentVersion, state.current_content_version_id)


def _resolve_verification_hold(session, port, ids, context, *, task_id, operation_id):
    admin_run = _next(ids)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=admin_run,
        owner="Marco",
        agent="claude",
    )
    holds = port.execute(
        _call("holds", run_id=admin_run, owner="Marco", principal="admin")
    )
    row = next(item for item in holds.data["holds"] if item["task_id"] == str(task_id))
    resolved = port.execute(
        _call(
            "resolved",
            run_id=admin_run,
            request_id=_next(ids),
            owner="Marco",
            principal="admin",
            arguments={"submission_id": operation_id},
        )
    )
    return row, resolved


def test_third_large_rejection_enters_verification_hold_and_resolved_reopens_cycle(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, _prepared, _inspection = _verification_ready(
            session, ids, context, task_id
        )
        operation_id = started.data["operation_id"]
        first = _reject_large_round(
            port, ids, task_id=task_id, operation_id=operation_id,
            run_id=verifier_run, owner="verifier-owner", agent="codex", round_number=1,
        )
        second_run = _open_verifier_round(
            session, port, ids, context, task_id=task_id, operation_id=operation_id,
            owner="verifier-2", agent="gpt",
        )
        second = _reject_large_round(
            port, ids, task_id=task_id, operation_id=operation_id,
            run_id=second_run, owner="verifier-2", agent="gpt", round_number=2,
        )
        third_run = _open_verifier_round(
            session, port, ids, context, task_id=task_id, operation_id=operation_id,
            owner="verifier-3", agent="codex",
        )
        third = _reject_large_round(
            port, ids, task_id=task_id, operation_id=operation_id,
            run_id=third_run, owner="verifier-3", agent="codex", round_number=3,
        )
        assert first.ok and first.data["verification_hold"] is False
        assert second.ok and second.data["verification_hold"] is False
        assert third.ok and third.data["verification_hold"] is True
        assert third.data["new_cycle_id"] is None
        operation = session.get(wf.WorkflowOperation, uuid.UUID(operation_id))
        assert operation.phase == "held_human"
        held = _active_content(session, context, task_id)
        parse_canonical_document(
            title=held.title, body=held.body, expected_status="pending-human-review"
        )
        row, resolved = _resolve_verification_hold(
            session, port, ids, context, task_id=task_id, operation_id=operation_id
        )
        assert row["hold_class"] == "verification_two_pass"
        assert row["required_admin_action"] == "resolved"
        assert row["cycle_id"] == third.data["cycle_id"]
        assert resolved.ok
        assert resolved.data["source_cycle_id"] == third.data["cycle_id"]
        session.refresh(operation)
        assert operation.phase == "await_verification"
        resumed = _active_content(session, context, task_id)
        parsed = parse_canonical_document(
            title=resumed.title, body=resumed.body, expected_status="pending-verification"
        )
        assert any(
            line.startswith("Human — Marco: Verification hold ")
            for line in parsed.document.decisions
        )


def test_evidence_hold_resumes_as_distinct_canonical_verification_occurrence(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, _prepared, _inspection = _verification_ready(
            session, ids, context, task_id
        )
        operation_id = started.data["operation_id"]
        rejected = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": operation_id,
                    "agent": "codex",
                    "route": "evidence",
                    "reason": "Need an exact source for the hydration claim",
                },
            )
        )
        assert rejected.ok
        assert rejected.data["route"] == "evidence"
        held_version = session.get(
            models.ContentVersion,
            uuid.UUID(rejected.data["held_content_version_id"]),
        )
        parse_canonical_document(
            title=held_version.title,
            body=held_version.body,
            expected_status="pending-evidence",
        )

        admin_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        supplied = port.execute(
            _call(
                "supply-evidence",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "submission_id": operation_id,
                    "detail": "The selected dry route is supported by the hydration source",
                    "resume_status": "pending-verification",
                    "expected_task_gid": "123456789",
                    "expected_cycle_id": rejected.data["cycle_id"],
                },
            )
        )
        assert supplied.ok
        assert supplied.data["hold_id"] == rejected.data["hold_id"]
        assert supplied.data["cycle_id"] != rejected.data["cycle_id"]
        hold = session.get(wf.EvidenceHold, uuid.UUID(rejected.data["hold_id"]))
        operation = session.get(wf.WorkflowOperation, uuid.UUID(operation_id))
        assert hold.state == "supplied"
        assert operation.phase == "await_verification"
        state = session.get(models.DishState, (context["generation_id"], task_id))
        resumed = session.get(models.ContentVersion, state.current_content_version_id)
        parsed = parse_canonical_document(
            title=resumed.title,
            body=resumed.body,
            expected_status="pending-verification",
        )
        assert resumed.content_version_id != held_version.content_version_id
        assert any(
            line.startswith("Human — Marco: evidence resolved — The selected dry route")
            for line in parsed.document.decisions
        )
