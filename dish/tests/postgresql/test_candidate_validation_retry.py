"""Rejected caller content can be corrected without changing workflow identity."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg.command_port import CommandRuleError
from dish_pg.database import session_scope
from dish_pg.document_authority import CanonicalDocumentError
from dish_service.action_guidance import attach_connected_agent_guidance
from dish_service.native_mcp_server import NativeMCPAdapter
from tests.support.canonical import TASK
from tests.support.postgresql.command import (
    _add_verification_queue, _call, _port, _start_initial, _verification_ready,
)
from tests.support.postgresql.workflow import _next, _register_run, workflow_db


def _versions(session):
    return session.scalar(select(func.count()).select_from(models.ContentVersion))


def test_research_candidate_corrected_retry_and_exact_replay(workflow_db):
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run)
        operation_id = started.data["operation_id"]
        args = {
            "task_id": str(task_id), "operation_id": operation_id,
            "file_text": TASK.replace("Purpose: Compare texture", "Purpose: "),
            "agent": "claude", "model": "test-model",
        }
        before = _versions(session)
        call = _call("prepare", run_id=run, request_id=_next(ids), arguments=args)
        rejected = port.execute(call)
        assert not rejected.ok and rejected.code == "VALIDATION_FAILED"
        assert _versions(session) == before
        assert rejected.submission_id == operation_id
        assert "prepare" in rejected.allowed_actions
        retry = rejected.data["retry"]
        assert retry["action"] == "prepare"
        assert retry["same_run"] and retry["same_operation"]
        assert retry["fresh_request_id"] and not retry["same_cycle"]
        replay = port.execute(call)
        assert replay.request_replayed
        assert replay.data == rejected.data
        assert replay.allowed_actions == rejected.allowed_actions
        # The corrected request must use a new identity, retaining run/operation.
        corrected = port.execute(_call(
            "prepare", run_id=run, request_id=_next(ids),
            arguments={**args, "file_text": TASK},
        ))
        assert corrected.ok, corrected.data
        assert corrected.submission_id == operation_id


def test_verification_candidate_retry_preserves_cycle_and_guidance(workflow_db):
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _, run, started, prepared, _ = _verification_ready(
            session, ids, context, task_id,
        )
        operation_id = started.data["operation_id"]
        reviewed = session.get(models.ContentVersion, uuid.UUID(prepared.data["content_version_id"]))
        candidate = reviewed.title + "\n" + reviewed.body
        args = {
            "task_id": str(task_id), "operation_id": operation_id,
            "agent": "codex", "model": "test-verifier", "correction": "small",
            "reviewed_identity": reviewed.content_identity,
            "semantic_review_complete": True, "provenance_complete": True,
            "file_text": candidate.replace("Status: pending-verification", "Status: ready"),
        }
        before = _versions(session)
        cycle = port._latest_cycle(uuid.UUID(operation_id))
        cycle_id = cycle.cycle_id
        rejected = port.execute(_call(
            "approve", run_id=run, request_id=_next(ids), arguments=args,
            principal="verification", owner="verifier-owner",
        ))
        assert not rejected.ok and rejected.code == "VALIDATION_FAILED"
        assert _versions(session) == before
        assert port._latest_cycle(uuid.UUID(operation_id)).cycle_id == cycle_id
        assert cycle.outcome is None
        assert "approve" in rejected.allowed_actions
        assert rejected.submission_id == operation_id
        retry = rejected.data["retry"]
        assert retry["same_cycle"] and retry["same_operation"] and retry["same_run"]
        findings = rejected.data["errors"]
        status = next(item for item in findings if item["rule"] == "state.status-required")
        assert status["location"] == "State.Status"
        assert status["expected"] == "pending-verification"
        assert status["recovery"]
        rendered = attach_connected_agent_guidance({
            "ok": False, "command": "approve", "code": rejected.code,
            "allowed_actions": list(rejected.allowed_actions), "data": dict(rejected.data),
        })
        assert retry["instruction"] in rendered["data"]["agent_guidance"]["instructions"]
        class ReplyService:
            def execute_agent(self, *args, **kwargs):
                return rendered

        adapter = NativeMCPAdapter(ReplyService(), owner_id="test-owner")
        mcp_result = adapter.call(
            "dish_approve",
            {"client": {"run_id": str(run), "request_id": str(_next(ids))},
             "arguments": {
                 "submission_id": operation_id, "agent": "codex", "model": "test-verifier",
                 "correction": "small", "reviewed_identity": reviewed.content_identity,
                 "semantic_review_complete": True, "provenance_complete": True,
                 "file_text": args["file_text"],
             }},
            caller={"principal_class": "connected-agent", "subject": "test-owner"},
        )
        assert mcp_result["data"]["retry"] == retry
        assert retry["instruction"] in mcp_result["data"]["agent_guidance"]["instructions"]
        corrected = port.execute(_call(
            "approve", run_id=run, request_id=_next(ids),
            arguments={**args, "file_text": candidate.replace(
                "1. Cook it.", "1. Cook it.\n2. Finish freshly just before serving.",
            )},
            principal="verification", owner="verifier-owner",
        ))
        assert corrected.ok, corrected.data
        assert port._latest_cycle(uuid.UUID(operation_id)).cycle_id == cycle_id


@pytest.mark.parametrize("findings", [
    [],
    [{"kind": "syntax"}],
    [{"kind": "unknown", "recovery": "Do something"}],
    [{"kind": "syntax", "recovery": "Fix syntax"}, {"kind": "human-decision", "recovery": "Ask"}],
])
def test_nonrepairable_or_mixed_candidate_findings_have_no_retry(workflow_db, findings):
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run)
        with patch("dish_pg.command_port.prepared_document", side_effect=CanonicalDocumentError(
            "candidate invalid", errors=findings,
        )):
            rejected = port.execute(_call(
                "prepare", run_id=run, request_id=_next(ids),
                arguments={"task_id": str(task_id), "operation_id": started.data["operation_id"],
                           "file_text": TASK, "agent": "claude", "model": "test"},
            ))
        assert rejected.code == "VALIDATION_FAILED"
        assert "retry" not in rejected.data
        assert rejected.data["errors"] == findings


@pytest.mark.parametrize("failure", [
    CanonicalDocumentError("stored content invalid", errors=[{"kind": "syntax", "recovery": "Fix"}]),
    CommandRuleError("BACKEND_UNCERTAIN", "uncertain outcome"),
    CommandRuleError("RELEASE_MISMATCH", "release mismatch"),
])
def test_unproven_candidate_or_uncertain_failure_has_no_retry(workflow_db, failure):
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run)
        with patch.object(port, "_apply", side_effect=failure):
            rejected = port.execute(_call(
                "prepare", run_id=run, request_id=_next(ids),
                arguments={"task_id": str(task_id), "operation_id": started.data["operation_id"],
                           "file_text": TASK, "agent": "claude", "model": "test"},
            ))
        assert not rejected.ok
        assert "retry" not in rejected.data


def test_illegal_action_does_not_validate_or_offer_retry(workflow_db):
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _, run, started, _, _ = _verification_ready(session, ids, context, task_id)
        with patch("dish_pg.command_port.prepared_document") as validator:
            rejected = port.execute(_call(
                "prepare", run_id=run, request_id=_next(ids), owner="verifier-owner",
                arguments={"task_id": str(task_id), "operation_id": started.data["operation_id"],
                           "file_text": TASK, "agent": "codex", "model": "test"},
            ))
        assert not rejected.ok
        assert "retry" not in rejected.data
        validator.assert_not_called()


def test_candidate_rejection_with_no_legal_continuation_offers_no_retry(workflow_db):
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run)
        with patch.object(port, "_continuation_envelope", side_effect=lambda **kw: (
            dict(kw["data"]), {"allowed_actions": []},
        )):
            rejected = port.execute(_call(
                "prepare", run_id=run, request_id=_next(ids),
                arguments={"task_id": str(task_id), "operation_id": started.data["operation_id"],
                           "file_text": TASK.replace("Purpose: Compare texture", "Purpose: "),
                           "agent": "claude", "model": "test"},
            ))
        assert rejected.code == "VALIDATION_FAILED"
        assert not rejected.allowed_actions
        assert "retry" not in rejected.data
