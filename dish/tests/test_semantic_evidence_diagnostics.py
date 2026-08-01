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
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.step7 import assert_verifier_authority
from tests.support.thread_teardown import join_thread, managed_thread
from tests.support.semantic_evidence import insert_operation as _insert_operation



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
    problem = next(
        problem
        for problem in exc.value.details["problems"]
        if problem["invariant"] == "content_identity_mismatch"
    )
    assert problem["record_type"] == "content_versions"
    assert problem["record_id"] == "content-version-42"
    assert problem["mutation_provenance"] == {
        "task_gid": "task-op",
        "operation_id": "op",
    }
    assert problem["timestamps"] == {"created_at": "2026-07-28T00:02:00Z"}
    assert problem["broken_relationship"] == {
        "source_fields": ["title", "notes"],
        "targets": [{
            "record_type": "content_versions",
            "record_id": "content-version-42",
            "fields": ["identity"],
        }],
        "required_predicate": "content_digest(title, notes) == identity",
    }
    assert exc.value.details["transaction_state"] == {
        "connection_in_transaction": False,
        "evidence_visibility": "committed_database",
    }
    assert exc.value.details["diagnostic_timestamp"].endswith("Z")
    rendered = repr(exc.value.details)
    assert "SENSITIVE RAW IDENTITY" not in rendered
    assert "SENSITIVE RAW TITLE" not in rendered
    assert "SENSITIVE RAW NOTES" not in rendered
    assert "title" not in problem.get("mutation_provenance", {})
    assert "notes" not in problem.get("mutation_provenance", {})
    assert "identity" not in problem.get("mutation_provenance", {})
def test_semantic_evidence_diagnostic_marks_connection_local_transaction(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """INSERT INTO content_versions(
               content_version_id,task_gid,operation_id,boundary,identity,
               title,notes,confirmed,created_at
           ) VALUES('local-invalid','task-local',NULL,'candidate','bad',
                    'LOCAL TITLE','LOCAL NOTES',1,'2026-07-28T00:04:00Z')"""
    )

    with pytest.raises(DishRuleError) as exc:
        database_schema_module._validate_semantic_evidence(conn)

    assert exc.value.rule == "database_semantic_evidence_invalid"
    assert exc.value.details["transaction_state"] == {
        "connection_in_transaction": True,
        "evidence_visibility": "connection_local_uncommitted",
    }
    assert "LOCAL TITLE" not in repr(exc.value.details)
    assert "LOCAL NOTES" not in repr(exc.value.details)
    conn.rollback()
    conn.close()
def test_semantic_evidence_document_failure_omits_raw_json(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    _insert_operation(conn)
    conn.execute(
        """INSERT INTO operation_executions(
               execution_id,operation_id,command,baseline_json,status,
               evidence_json,created_at,completed_at,resolved_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            "execution-42",
            "op",
            "prepare",
            "{}",
            "completed",
            '["SENSITIVE RAW RECOVERY PAYLOAD"]',
            "2026-07-28T00:02:00Z",
            "2026-07-28T00:03:00Z",
            "2026-07-28T00:04:00Z",
        ),
    )
    conn.close()

    with pytest.raises(DishRuleError) as exc:
        initialize_database(db_path)

    assert exc.value.rule == "database_semantic_evidence_invalid"
    problem = next(
        problem
        for problem in exc.value.details["problems"]
        if problem["invariant"] == "operation_execution_evidence_document"
    )
    assert problem["record_type"] == "operation_executions"
    assert problem["record_id"] == "execution-42"
    assert problem["mutation_provenance"] == {
        "operation_id": "op",
        "execution_id": "execution-42",
        "command": "prepare",
    }
    assert problem["timestamps"] == {
        "created_at": "2026-07-28T00:02:00Z",
        "completed_at": "2026-07-28T00:03:00Z",
        "resolved_at": "2026-07-28T00:04:00Z",
    }
    assert problem["broken_relationship"]["required_predicate"] == (
        "evidence_json is a JSON object"
    )
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

    threads = [managed_thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        join_thread(thread, timeout=5)
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
def test_cycle_sequence_diagnostic_keeps_task_provenance(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    _insert_operation(conn)
    for cycle_id, cycle_number in (("cycle-1", 1), ("cycle-3", 3)):
        conn.execute(
            """INSERT INTO verification_cycles(
                   cycle_id,operation_id,task_gid,cycle_number,
                   protocol_release,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                cycle_id,
                "op",
                "task-op",
                cycle_number,
                "1",
                f"2026-07-28T00:0{cycle_number}:00Z",
            ),
        )
    conn.close()

    with pytest.raises(DishRuleError) as exc:
        initialize_database(db_path)

    assert exc.value.rule == "database_semantic_evidence_invalid"
    problem = next(
        problem
        for problem in exc.value.details["problems"]
        if problem["invariant"] == "verification_cycle_sequence"
    )
    assert problem["record_type"] == "task_verification_cycles"
    assert problem["record_id"] == "task-op"
    assert problem["mutation_provenance"] == {"task_gid": "task-op"}
    assert problem["broken_relationship"]["required_predicate"] == (
        "cycle_number values are the contiguous sequence 1..max(cycle_number)"
    )
def test_small_correction_lineage_diagnostic_names_exact_relationship(tmp_path):
    from dish_tool.database import content_identity

    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    _insert_operation(conn)
    reviewed_title, reviewed_notes = "Dish", "Reviewed notes"
    signed_title, signed_notes = "Dish", "Corrected notes"
    reviewed_identity = content_identity(reviewed_title, reviewed_notes).digest
    signed_identity = content_identity(signed_title, signed_notes).digest
    for version_id, identity, title, notes, created_at in (
        ("reviewed-version", reviewed_identity, reviewed_title, reviewed_notes, "2026-07-28T00:01:00Z"),
        ("signed-version", signed_identity, signed_title, signed_notes, "2026-07-28T00:02:00Z"),
    ):
        conn.execute(
            """INSERT INTO content_versions(
                   content_version_id,task_gid,operation_id,boundary,identity,
                   title,notes,confirmed,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (version_id, "task-op", "op", "verification", identity, title, notes, 1, created_at),
        )
    conn.execute(
        """INSERT INTO verification_cycles(
               cycle_id,operation_id,task_gid,cycle_number,protocol_release,
               verifier_agent,run_id,correction_class,outcome,created_at,completed_at,
               reviewed_content_version_id,reviewed_identity,
               signed_content_version_id,signed_identity
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cycle-small", "op", "task-op", 1, "1", "codex", "review-run",
            "small", "approved", "2026-07-28T00:03:00Z", "2026-07-28T00:04:00Z",
            "reviewed-version", reviewed_identity, "signed-version", signed_identity,
        ),
    )
    conn.close()

    with pytest.raises(DishRuleError) as exc:
        initialize_database(db_path)

    assert exc.value.rule == "database_semantic_evidence_invalid"
    problem = next(
        problem
        for problem in exc.value.details["problems"]
        if problem["invariant"] == "small_correction_lineage"
    )
    assert problem["mutation_provenance"] == {
        "task_gid": "task-op",
        "operation_id": "op",
        "run_id": "review-run",
        "cycle_id": "cycle-small",
    }
    assert problem["timestamps"] == {
        "created_at": "2026-07-28T00:03:00Z",
        "completed_at": "2026-07-28T00:04:00Z",
    }
    relationship = problem["broken_relationship"]
    assert [target["record_type"] for target in relationship["targets"]] == [
        "write_attempts", "content_versions", "write_attempts"
    ]
    assert relationship["required_predicate"].startswith(
        "an approved small correction whose reviewed and signed identities differ"
    )
