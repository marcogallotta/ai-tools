from __future__ import annotations

import sqlite3
import uuid

import pytest

from dish_service.leases import ServicePrincipal
from dish_tool.database_schema import initialize_database
from tests.support.operational import _service
from tests.support.verification import TASK


def _reviewed_service(tmp_path):
    service, backend = _service(tmp_path)
    constructor = ServicePrincipal("constructor", "constructor-run")
    verifier = ServicePrincipal("verifier", "verifier-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": constructor.run_id},
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    assert service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )["ok"]
    review = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "run_id": verifier.run_id,
            "independence_attestation": "independent",
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert review["ok"]
    assert service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )["ok"]
    return service, backend, operation_id, constructor, verifier, review


def _fail_event_once(monkeypatch, module, event_type: str):
    original = module.record_audit
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed and kwargs.get("event_type") == event_type:
            failed = True
            raise sqlite3.OperationalError(f"simulated {event_type} failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "record_audit", fail_once)
    return original


def _assert_uncertain_without_claim(service, operation_id: str, request_id: str):
    conn = initialize_database(service.config.db_path)
    try:
        execution = conn.execute(
            "SELECT status,resolved_at FROM operation_executions WHERE operation_id=? AND request_id=?",
            (operation_id, request_id),
        ).fetchone()
        assert tuple(execution) == ("uncertain", None)
        assert conn.execute(
            "SELECT 1 FROM operation_execution_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone() is None
    finally:
        conn.close()


def _assert_resolved_once(service, operation_id: str, request_id: str, event_type: str):
    conn = initialize_database(service.config.db_path)
    try:
        execution = conn.execute(
            "SELECT status,resolved_at FROM operation_executions WHERE operation_id=? AND request_id=?",
            (operation_id, request_id),
        ).fetchone()
        assert execution["status"] == "completed"
        assert execution["resolved_at"] is not None
        request = conn.execute(
            "SELECT status,resolution_result_json,resolved_at FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert request["status"] == "completed"
        assert request["resolution_result_json"] is not None
        assert request["resolved_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type=?",
            (operation_id, event_type),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_approval_missing_decision_audit_fences_and_exact_replay_recovers(
    tmp_path, monkeypatch
):
    import dish_tool.step7 as step7

    service, _backend, operation_id, _constructor, verifier, review = _reviewed_service(tmp_path)
    original = _fail_event_once(monkeypatch, step7, "verification.approved")
    request_id = str(uuid.uuid4())
    arguments = {
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "correction": "none",
        "reviewed_identity": review["data"]["reviewed_identity"],
        "semantic_review_complete": True,
        "provenance_complete": True,
        "run_id": verifier.run_id,
    }
    failed = service.execute_agent(
        "approve", arguments, principal=verifier, request_id=request_id
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    _assert_uncertain_without_claim(service, operation_id, request_id)

    blocked = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert blocked["errors"][0]["rule"] == "operation_mutation_recovery_required"

    monkeypatch.setattr(step7, "record_audit", original)
    recovered = service.execute_agent(
        "approve", arguments, principal=verifier, request_id=request_id
    )
    assert recovered["ok"]
    assert recovered["data"]["approval_recovered"] is True
    _assert_resolved_once(service, operation_id, request_id, "verification.approved")


def test_rejection_missing_decision_audit_fences_and_exact_replay_recovers(
    tmp_path, monkeypatch
):
    import dish_tool.step8 as step8

    service, _backend, operation_id, _constructor, verifier, _review = _reviewed_service(tmp_path)
    original = _fail_event_once(monkeypatch, step8, "verification.rejected")
    request_id = str(uuid.uuid4())
    arguments = {
        "agent": "codex",
        "submission_id": operation_id,
        "route": "evidence",
        "reason": "Marco must confirm the factual input",
        "resume_status": "pending-verification",
        "run_id": verifier.run_id,
    }
    failed = service.execute_agent(
        "reject", arguments, principal=verifier, request_id=request_id
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    _assert_uncertain_without_claim(service, operation_id, request_id)

    blocked = service.execute_agent(
        "reject", arguments, principal=verifier, request_id=str(uuid.uuid4())
    )
    assert blocked["errors"][0]["rule"] == "operation_mutation_recovery_required"

    monkeypatch.setattr(step8, "record_audit", original)
    recovered = service.execute_agent(
        "reject", arguments, principal=verifier, request_id=request_id
    )
    assert recovered["ok"]
    assert recovered["data"]["rejection_recovered"] is True
    _assert_resolved_once(service, operation_id, request_id, "verification.rejected")


def test_destination_failure_classification_missing_audit_fences_and_replays(
    tmp_path, monkeypatch
):
    import dish_tool.step9 as step9

    service, backend, operation_id, _constructor, verifier, review = _reviewed_service(tmp_path)
    approved = service.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "correction": "none",
            "reviewed_identity": review["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
            "run_id": verifier.run_id,
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert approved["ok"]
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    original = _fail_event_once(
        monkeypatch, step9, "operation.destination_movement_failed"
    )
    request_id = str(uuid.uuid4())
    failed = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=request_id,
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    _assert_uncertain_without_claim(service, operation_id, request_id)

    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["allowed_actions"] == []
    assert inspected["data"]["authoritative_view"]["unresolved_execution_ids"]

    monkeypatch.setattr(step9, "record_audit", original)
    replayed = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=request_id,
    )
    assert replayed["code"] == "VALIDATION_FAILED"
    assert replayed["errors"][0]["rule"] == "destination_movement_unresolvable"
    _assert_resolved_once(
        service, operation_id, request_id, "operation.destination_movement_failed"
    )


def test_destination_repair_missing_audit_fences_and_exact_replay_recovers(
    tmp_path, monkeypatch
):
    import dish_tool.step9 as step9

    service, backend, operation_id, _constructor, verifier, review = _reviewed_service(tmp_path)
    assert service.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "correction": "none",
            "reviewed_identity": review["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
            "run_id": verifier.run_id,
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )["ok"]
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    failed_submit = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert failed_submit["errors"][0]["rule"] == "destination_movement_unresolvable"
    backend.sections.append({"gid": "67890", "name": "Hunan"})

    original = _fail_event_once(monkeypatch, step9, "operation.destination_repaired")
    request_id = str(uuid.uuid4())
    marco = verifier
    arguments = {
        "submission_id": operation_id,
        "destination_section_gid": "67890",
        "reason": "approved destination was deleted",
    }
    failed = service.execute_admin(
        "repair-destination", arguments, principal=marco, request_id=request_id
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    _assert_uncertain_without_claim(service, operation_id, request_id)

    blocked = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert blocked["errors"][0]["rule"] == "operation_mutation_recovery_required"

    monkeypatch.setattr(step9, "record_audit", original)
    recovered = service.execute_admin(
        "repair-destination", arguments, principal=marco, request_id=request_id
    )
    assert recovered["ok"]
    _assert_resolved_once(
        service, operation_id, request_id, "operation.destination_repaired"
    )


def test_cancellation_missing_audit_fences_and_exact_replay_recovers(
    tmp_path, monkeypatch
):
    import dish_tool.admin as admin

    service, _backend = _service(tmp_path)
    constructor = ServicePrincipal("constructor", "constructor-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": constructor.run_id},
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    operation_id = started["submission_id"]
    original = _fail_event_once(monkeypatch, admin, "operation.cancelled")
    request_id = str(uuid.uuid4())
    marco = constructor
    arguments = {"submission_id": operation_id, "reason": "abandon clean operation"}
    failed = service.execute_admin(
        "discard", arguments, principal=marco, request_id=request_id
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    _assert_uncertain_without_claim(service, operation_id, request_id)

    blocked = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert blocked["errors"][0]["rule"] == "operation_mutation_recovery_required"

    monkeypatch.setattr(admin, "record_audit", original)
    recovered = service.execute_admin(
        "discard", arguments, principal=marco, request_id=request_id
    )
    assert recovered["ok"]
    _assert_resolved_once(service, operation_id, request_id, "operation.cancelled")
