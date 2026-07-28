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
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.step7 import assert_verifier_authority


def _insert_operation(conn, operation_id="op", *, status="open", phase="prepare_required"):
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,expected_identity,
               schema_version,created_at,phase,expected_section_gid,
               completed_at,terminal_outcome
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            f"task-{operation_id}",
            "initial",
            status,
            "identity",
            "2",
            "2026-07-28T00:00:00Z",
            phase,
            "research",
            "2026-07-28T00:01:00Z" if status in {"completed", "cancelled"} else None,
            "test" if status in {"completed", "cancelled"} else None,
        ),
    )


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

    thread = threading.Thread(target=run_first)
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
    thread.join(timeout=5)
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
    assert {
        "invariant": "active_lease_on_incomplete_terminal_operation",
        "record_type": "service_leases",
        "record_id": lease["lease_id"],
        "related_record_type": "operations",
        "related_record_id": "op",
    } in exc.value.details["problems"]


def test_semantic_evidence_diagnostic_omits_raw_content_payload(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    _insert_operation(conn)
    conn.execute(
        """INSERT INTO content_versions(
               content_version_id,task_gid,operation_id,boundary,identity,
               title,notes,confirmed,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            "content-version-42",
            "task-op",
            "op",
            "candidate",
            "SENSITIVE RAW IDENTITY",
            "SENSITIVE RAW TITLE",
            "SENSITIVE RAW NOTES",
            1,
            "2026-07-28T00:02:00Z",
        ),
    )
    conn.close()

    with pytest.raises(DishRuleError) as exc:
        initialize_database(db_path)

    assert exc.value.rule == "database_semantic_evidence_invalid"
    assert {
        "invariant": "content_identity_mismatch",
        "record_type": "content_versions",
        "record_id": "content-version-42",
    } in exc.value.details["problems"]
    rendered = repr(exc.value.details)
    assert "SENSITIVE RAW IDENTITY" not in rendered
    assert "SENSITIVE RAW TITLE" not in rendered
    assert "SENSITIVE RAW NOTES" not in rendered
    assert all(
        set(problem).issubset({
            "invariant",
            "record_type",
            "record_id",
            "related_record_type",
            "related_record_id",
            "observed_count",
        })
        for problem in exc.value.details["problems"]
    )


def test_semantic_evidence_document_failure_omits_raw_json(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    _insert_operation(conn)
    conn.execute(
        """INSERT INTO operation_executions(
               execution_id,operation_id,command,baseline_json,status,
               evidence_json,created_at,completed_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "execution-42",
            "op",
            "prepare",
            "{}",
            "completed",
            '["SENSITIVE RAW RECOVERY PAYLOAD"]',
            "2026-07-28T00:02:00Z",
            "2026-07-28T00:03:00Z",
        ),
    )
    conn.close()

    with pytest.raises(DishRuleError) as exc:
        initialize_database(db_path)

    assert {
        "invariant": "operation_execution_evidence_document",
        "record_type": "operation_executions",
        "record_id": "execution-42",
    } in exc.value.details["problems"]
    assert "SENSITIVE RAW RECOVERY PAYLOAD" not in repr(exc.value.details)


def test_verifier_decision_requires_exact_start_attestation():
    cycle = {
        "verifier_agent": "codex",
        "run_id": "review-run",
        "independence_attestation": "independent fresh review",
    }
    with pytest.raises(DishRuleError) as exc:
        assert_verifier_authority(
            cycle,
            agent="codex",
            run_id="review-run",
            independence_attestation="different attestation",
        )
    assert exc.value.rule == "verifier_proof_mismatch"
    assert exc.value.details["run_id_matches"] is True
    assert exc.value.details["independence_attestation_matches"] is False


def test_concurrent_audit_repair_workers_emit_one_event(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    repair_id = record_command_audit_repair(
        conn,
        command="read",
        result={"ok": True, "code": "OK", "state": "read", "retryable": False},
        audit_error="simulated",
    )
    conn.close()

    barrier = threading.Barrier(2)
    results = []

    def worker():
        worker_conn = initialize_database(db_path)
        try:
            barrier.wait()
            results.append(process_command_audit_repairs(worker_conn))
        finally:
            worker_conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [0, 1]

    check = initialize_database(db_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM audit_events WHERE details LIKE ?",
            (f'%"repaired_from":"{repair_id}"%',),
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT repaired_at FROM command_audit_repairs WHERE repair_id=?",
            (repair_id,),
        ).fetchone()[0]
    finally:
        check.close()
