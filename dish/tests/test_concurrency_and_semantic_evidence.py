from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool import database as database_module
from dish_tool.application_service import CurrentWorkflowService
from dish_tool.database import (
    process_command_audit_repairs,
    record_command_audit_repair,
    transition_operation,
)
from dish_tool import database_schema as database_schema_module
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.step7 import assert_verifier_authority
from tests.support.thread_teardown import join_thread, managed_thread
from tests.support.semantic_evidence import insert_operation as _insert_operation



def test_only_one_operation_mutation_executor_can_run(tmp_path):
    db_path = tmp_path / "dish.db"
    seed = initialize_database(db_path)
    _insert_operation(seed)
    seed.close()

    first_entered = threading.Event()
    first_release = threading.Event()
    first_result = []

    def run_first():
        conn = initialize_database(db_path)
        service = CurrentWorkflowService(conn, object())
        service.assert_action = lambda *args, **kwargs: None
        service._post_operation_view = lambda operation_id, result, schema=None: (result, {})
        try:
            first_result.append(
                service.mutate(
                    "op",
                    "prepare",
                    lambda: (first_entered.set(), first_release.wait(5), {"ok": True})[-1],
                )
            )
        finally:
            conn.close()

    thread = managed_thread(target=run_first)
    thread.start()
    assert first_entered.wait(5)

    second_conn = initialize_database(db_path)
    second = CurrentWorkflowService(second_conn, object())
    second.assert_action = lambda *args, **kwargs: None
    second._post_operation_view = lambda operation_id, result, schema=None: (result, {})
    second_called = False

    def second_executor():
        nonlocal second_called
        second_called = True
        return {"ok": True}

    with pytest.raises(DishRuleError) as exc:
        second.mutate("op", "prepare", second_executor)
    assert exc.value.rule == "operation_mutation_in_progress"
    assert exc.value.retryable is True
    assert second_called is False
    second_conn.close()

    first_release.set()
    join_thread(thread, timeout=5)
    assert not thread.is_alive()
    assert first_result and first_result[0][0] == {"ok": True}

    check = initialize_database(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM operation_execution_claims").fetchone()[0] == 0
    finally:
        check.close()
def test_transition_and_required_audit_are_atomic(monkeypatch, tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    _insert_operation(conn)

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("simulated audit failure")

    monkeypatch.setattr(database_module, "record_audit", fail_audit)
    with pytest.raises(sqlite3.OperationalError):
        transition_operation(conn, "op", phase="await_verification")

    row = conn.execute("SELECT phase,status FROM operations WHERE operation_id='op'").fetchone()
    assert tuple(row) == ("prepare_required", "open")
    assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    conn.close()
def test_unresolved_attempts_are_unique_per_operation(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    _insert_operation(conn)
    conn.execute(
        """INSERT INTO write_attempts(
               attempt_id,operation_id,expected_identity,outcome,started_at
           ) VALUES('one','op','identity','started','now')"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO write_attempts(
                   attempt_id,operation_id,expected_identity,outcome,started_at
               ) VALUES('two','op','identity','uncertain','later')"""
        )
    conn.close()
def test_terminal_operation_may_keep_non_authoritative_cleanup_lease(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    _insert_operation(conn)
    lease = LeaseManager(conn).acquire("op", ServicePrincipal("owner", "run"))
    assert lease is not None
    conn.execute(
        """UPDATE operations
              SET status='completed',phase='terminal',completed_at='now',terminal_outcome='test'
            WHERE operation_id='op'"""
    )
    conn.close()

    reopened = initialize_database(db_path)
    try:
        active = reopened.execute(
            "SELECT lease_id FROM service_leases WHERE operation_id='op' AND released_at IS NULL"
        ).fetchone()
        assert active is not None
    finally:
        reopened.close()
def test_terminal_operation_with_incomplete_evidence_remains_invalid(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    _insert_operation(conn)
    lease = LeaseManager(conn).acquire("op", ServicePrincipal("owner", "run"))
    conn.execute(
        "INSERT INTO operation_steps(operation_id,step_name,intended_json) "
        "VALUES('op','terminal_pending','{}')"
    )
    conn.execute(
        """UPDATE operations
              SET status='completed',phase='terminal',completed_at='now',
                  terminal_outcome='test'
            WHERE operation_id='op'"""
    )
    conn.close()

    with pytest.raises(DishRuleError) as exc:
        initialize_database(db_path)
    assert exc.value.rule == "database_semantic_evidence_invalid"
    problem = next(
        problem
        for problem in exc.value.details["problems"]
        if problem["invariant"] == "active_lease_on_incomplete_terminal_operation"
    )
    assert problem["record_type"] == "service_leases"
    assert problem["record_id"] == lease["lease_id"]
    assert problem["related_record_type"] == "operations"
    assert problem["related_record_id"] == "op"
    assert problem["mutation_provenance"] == {
        "task_gid": "task-op",
        "operation_id": "op",
        "run_id": "run",
        "owner_id": "owner",
    }
    assert problem["timestamps"]["acquired_at"]
    assert problem["broken_relationship"]["targets"][0] == {
        "record_type": "operations",
        "selector": {"operation_id": "op"},
        "fields": ["status", "phase", "completed_at", "terminal_outcome"],
    }
