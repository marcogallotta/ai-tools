from __future__ import annotations

import sqlite3
import uuid

import pytest

from dish_tool.database_initialization import initialize_database
from dish_tool.database_schema import _validate_semantic_evidence
from tests.support.operational import _approved, _service


def _submit_with_audit_failure(service, backend, operation_id, verifier, monkeypatch):
    import dish_tool.step9 as step9

    original = step9.record_audit
    failed_once = False

    def fail_submission_audit_once(*args, **kwargs):
        nonlocal failed_once
        if not failed_once and kwargs.get("event_type") == "operation.submitted":
            failed_once = True
            raise sqlite3.OperationalError("simulated operation.submitted failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(step9, "record_audit", fail_submission_audit_once)
    request_id = str(uuid.uuid4())
    moves_before = backend.moves
    failed = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=request_id,
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert backend.moves == moves_before + 1
    return step9, original, request_id, moves_before


def _assert_partial_submission_state(service, operation_id, request_id):
    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT status,phase,movement_completed_at,completed_at FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(operation[:2]) == ("open", "await_submission")
        assert operation["movement_completed_at"] is not None
        assert operation["completed_at"] is None
        terminal_intent = conn.execute(
            "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='submission_terminal_intent'",
            (operation_id,),
        ).fetchone()
        assert terminal_intent is not None and terminal_intent["completed_at"] is None
        assert conn.execute(
            "SELECT 1 FROM audit_events WHERE operation_id=? AND event_type='operation.submitted'",
            (operation_id,),
        ).fetchone() is None
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


def _assert_terminal_submission_state(service, operation_id, request_id):
    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT status,phase,terminal_outcome,completed_at FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(operation[:3]) == ("completed", "terminal", "destination_handled")
        assert operation["completed_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='operation.submitted'",
            (operation_id,),
        ).fetchone()[0] == 1
        terminal_steps = conn.execute(
            "SELECT step_name,completed_at FROM operation_steps WHERE operation_id=? AND step_name IN ('submission_terminal_intent','submission_terminal')",
            (operation_id,),
        ).fetchall()
        assert {row["step_name"] for row in terminal_steps} == {
            "submission_terminal_intent",
            "submission_terminal",
        }
        assert all(row["completed_at"] is not None for row in terminal_steps)
        execution = conn.execute(
            "SELECT status,resolved_at FROM operation_executions WHERE operation_id=? AND request_id=?",
            (operation_id, request_id),
        ).fetchone()
        assert execution["status"] == "completed" and execution["resolved_at"] is not None
        request = conn.execute(
            "SELECT status,resolution_result_json,resolved_at FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert request["status"] == "completed"
        assert request["resolution_result_json"] is not None
        assert request["resolved_at"] is not None
        assert conn.execute(
            "SELECT 1 FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone() is None
        _validate_semantic_evidence(conn)
    finally:
        conn.close()


@pytest.mark.smoke
@pytest.mark.invariant_submission_terminal_proof
def test_submission_audit_failure_preserves_movement_and_exact_replay_finalizes_once(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    operation_id, verifier = _approved(service)
    step9, original, request_id, moves_before = _submit_with_audit_failure(
        service, backend, operation_id, verifier, monkeypatch
    )
    _assert_partial_submission_state(service, operation_id, request_id)

    fresh = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert fresh["errors"][0]["rule"] == "operation_mutation_recovery_required"

    monkeypatch.setattr(step9, "record_audit", original)
    recovered = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=request_id,
    )
    assert recovered["ok"]
    assert recovered["data"]["submission_recovered"] is True
    assert backend.moves == moves_before + 1
    _assert_terminal_submission_state(service, operation_id, request_id)

    replay = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=request_id,
    )
    assert replay["ok"]
    assert replay["data"]["request_replayed"] is True
    assert backend.moves == moves_before + 1
