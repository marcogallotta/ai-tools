import json
import sqlite3
from pathlib import Path

import pytest

from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database import (
    MIGRATIONS,
    confirm_task_content,
    content_identity,
    create_operation,
    create_verification_cycle,
    initialize_database,
    inspect_legacy_submissions,
    mark_operation_completion,
    finalize_confirmed_movement_attempt,
)
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from dish_tool.recovery import (
    begin_movement_attempt,
    begin_operation_write_attempt,
    finish_movement_attempt,
    finish_operation_write_attempt,
)


def test_redesigned_schema_is_idempotent_and_complete(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    from dish_tool.database import migrate_database

    migrate_database(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "operations",
        "task_content_state",
        "content_versions",
        "verification_cycles",
        "write_attempts",
        "movement_attempts",
        "audit_events",
        "legacy_submission_quarantine",
    } <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_content_identity_normalizes_crlf_only():
    a = content_identity("Dish\r\nName", "a\r\nb")
    b = content_identity("Dish\nName", "a\nb")
    assert a.digest == b.digest
    assert a.title == "Dish\nName"
    assert a.notes == "a\nb"
    assert content_identity("Dish ", "a\nb").digest != content_identity("Dish", "a\nb").digest
    assert content_identity("Dish", "a\nb\n").digest != content_identity("Dish", "a\nb").digest


def test_task_scope_identity_survives_operation_completion(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = confirm_task_content(
        conn,
        task_gid="task",
        title="Dish",
        notes="Body",
        schema_version="2",
    )
    operation = create_operation(
        conn,
        task_gid="task",
        operation_kind="research",
        expected_identity=identity.digest,
        schema_version="2",
        actors=OperationActors(editor_agent="claude", researcher_agent="claude", run_id="run-1"),
    )
    conn.execute(
        "UPDATE operations SET status='completed', phase='terminal', completed_at='2026-07-25T00:00:00Z' WHERE operation_id=?",
        (operation["operation_id"],),
    )
    state = conn.execute("SELECT * FROM task_content_state WHERE task_gid='task'").fetchone()
    assert state["last_confirmed_identity"] == identity.digest
    assert state["last_confirmed_title"] == "Dish"
    assert state["last_confirmed_notes"] == "Body"


def test_one_open_operation_per_task_and_stale_identity_are_atomic(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = confirm_task_content(
        conn, task_gid="task", title="Dish", notes="Body", schema_version="2"
    )
    first = create_operation(
        conn,
        task_gid="task",
        operation_kind="planning",
        expected_identity=identity.digest,
        schema_version="2",
    )
    with pytest.raises(DishRuleError) as duplicate:
        create_operation(
            conn,
            task_gid="task",
            operation_kind="research",
            expected_identity=identity.digest,
            schema_version="2",
        )
    assert duplicate.value.rule == "open_operation_exists"
    assert conn.execute("SELECT count(*) FROM operations WHERE task_gid='task'").fetchone()[0] == 1

    conn.execute("UPDATE operations SET status='completed', phase='terminal', completed_at='now' WHERE operation_id=?", (first["operation_id"],))
    confirm_task_content(
        conn, task_gid="task", title="Externally edited", notes="Body", schema_version="2"
    )
    with pytest.raises(DishRuleError) as stale:
        create_operation(
            conn,
            task_gid="task",
            operation_kind="research",
            expected_identity=identity.digest,
            schema_version="2",
        )
    assert stale.value.rule == "stale_content_identity"
    assert conn.execute("SELECT count(*) FROM operations WHERE status='open'").fetchone()[0] == 0


def test_markers_verification_cycle_and_attempts_are_independent_and_audited(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = confirm_task_content(
        conn, task_gid="task", title="Dish", notes="Body", schema_version="2"
    )
    operation = create_operation(
        conn,
        task_gid="task",
        operation_kind="verification",
        expected_identity=identity.digest,
        schema_version="2",
        actors=OperationActors(verifier_agent="gpt", run_id="verify-1", independence_attestation="independent"),
    )
    op_id = operation["operation_id"]
    cycle = create_verification_cycle(
        conn,
        operation_id=op_id,
        task_gid="task",
        cycle_number=2,
        protocol_release="1.0.10",
        verifier_agent="gpt",
        run_id="verify-1",
        independence_attestation="independent",
        correction_class="deterministic",
        outcome="corrected",
        route="human_review",
        resume_state="awaiting_human_review",
    )
    assert cycle["cycle_number"] == 2
    assert cycle["protocol_release"] == "1.0.10"
    assert cycle["verifier_agent"] == "gpt"

    marked = mark_operation_completion(conn, op_id, "content_write")
    assert marked["content_write_completed_at"]
    assert marked["signoff_completed_at"] is None
    assert marked["movement_completed_at"] is None
    with pytest.raises(sqlite3.IntegrityError, match="approved signed cycle"):
        mark_operation_completion(conn, op_id, "signoff")

    write_id = begin_operation_write_attempt(
        conn,
        operation_id=op_id,
        expected_identity=identity.digest,
        intended_identity="new-digest",
    )
    assert finish_operation_write_attempt(conn, attempt_id=write_id, outcome="uncertain")["outcome"] == "uncertain"
    move_id = begin_movement_attempt(
        conn,
        operation_id=op_id,
        expected_section_gid="old",
        intended_section_gid="new",
    )
    assert finalize_confirmed_movement_attempt(conn, attempt_id=move_id, live_section_gid="new")["outcome"] == "confirmed"

    events = conn.execute(
        "SELECT event_type, result_code, result_ok FROM audit_events WHERE operation_id=? ORDER BY rowid",
        (op_id,),
    ).fetchall()
    assert [row["event_type"] for row in events] == [
        "operation.created",
        "verification_cycle.created",
        "operation.marker",
        "write_attempt.started",
        "write_attempt.finished",
        "movement_attempt.started",
        "movement_attempt.reconciled",
    ]
    assert all(row["result_code"] == "OK" and row["result_ok"] == 1 for row in events)


def _build_v2_database(path: Path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        + MIGRATIONS[1]
        + "\nINSERT INTO schema_migrations VALUES (1, 't');\n"
        + MIGRATIONS[2]
        + "\nINSERT INTO schema_migrations VALUES (2, 't');\nPRAGMA user_version=2;"
    )
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, status, created_at
        ) VALUES ('legacy-open', 'task', 'planning', 'old', 'abc', '{}', '{}',
                  'claude', 'claude', 'ready', '2026-07-25T00:00:00Z')
        """
    )
    conn.close()


def test_legacy_nonterminal_rows_are_backed_up_and_quarantined(tmp_path):
    db_path = tmp_path / "dish.db"
    _build_v2_database(db_path)
    conn = initialize_database(db_path)
    backup = db_path.with_suffix(".db.legacy-v2.bak")
    assert backup.exists()
    backup_conn = sqlite3.connect(backup)
    try:
        assert backup_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == 2
        legacy_row = backup_conn.execute(
            "SELECT status FROM submissions WHERE submission_id='legacy-open'"
        ).fetchone()
        assert legacy_row == ("ready",)
    finally:
        backup_conn.close()

    rows = inspect_legacy_submissions(conn, task_gid="task")
    assert len(rows) == 1
    assert rows[0]["legacy_status"] == "ready"
    payload = json.loads(rows[0]["row_json"])
    assert payload["status"] == "ready"
    assert conn.execute("SELECT status FROM submissions WHERE submission_id='legacy-open'").fetchone()[0] == "discarded"
    assert conn.execute("SELECT count(*) FROM task_content_state WHERE task_gid='task'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM operations WHERE task_gid='task'").fetchone()[0] == 0
