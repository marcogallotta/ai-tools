import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.admin import DishAdminApplication
from dish_tool.database import content_identity
from dish_tool.recovery import begin_movement_attempt, begin_operation_write_attempt
from test_dish_tool_step7_verification import make_app


def _prepared(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    return app, backend, operation_id


def test_started_write_is_reconciled_from_exact_live_evidence(tmp_path):
    app, backend, operation_id = _prepared(tmp_path)
    before = content_identity(backend.title, backend.notes).digest
    title = backend.title
    notes = backend.notes.replace("Crisp and aromatic.", "Crisp and deeply aromatic.")
    intended = content_identity(title, notes).digest
    attempt_id = begin_operation_write_attempt(
        app.conn,
        operation_id=operation_id,
        expected_identity=before,
        intended_identity=intended,
        intended_title=title,
        intended_notes=notes,
        schema_version="2",
        purpose="content_write",
    )
    # Simulate process death after Asana accepted the write, before local finalization.
    backend.title, backend.notes = title, notes
    admin = DishAdminApplication(app.conn, backend=backend)
    result = admin.execute("recover", submission_id=operation_id, outcome="applied", reason="restart reconciliation")
    assert result["ok"]
    assert result["data"]["content_recovery_state"] == "reconciled_confirmed_content_write"
    row = app.conn.execute("SELECT outcome, confirmed_content_version_id FROM write_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    assert row["outcome"] == "confirmed" and row["confirmed_content_version_id"]
    state = app.conn.execute("SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'").fetchone()
    assert state["last_confirmed_identity"] == intended


def test_started_write_not_applied_is_closed_without_repeating_write(tmp_path):
    app, backend, operation_id = _prepared(tmp_path)
    before = content_identity(backend.title, backend.notes).digest
    title = backend.title
    notes = backend.notes + "extra\n"
    attempt_id = begin_operation_write_attempt(
        app.conn, operation_id=operation_id, expected_identity=before,
        intended_identity=content_identity(title, notes).digest,
        intended_title=title, intended_notes=notes, schema_version="2",
        purpose="content_write",
    )
    writes = backend.writes
    admin = DishAdminApplication(app.conn, backend=backend)
    result = admin.execute("recover", submission_id=operation_id, outcome="not-applied", reason="backend rejected")
    assert result["ok"] and backend.writes == writes
    assert app.conn.execute("SELECT outcome FROM write_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0] == "not_applied"


def test_destination_movement_recovery_binds_final_marker_to_attempt(tmp_path):
    app, backend, operation_id = _prepared(tmp_path)
    attempt_id = begin_movement_attempt(
        app.conn, operation_id=operation_id, expected_section_gid=backend.section,
        intended_section_gid="12345", purpose="destination_submission",
    )
    backend.section = "12345"  # external move succeeded; process died before recording
    admin = DishAdminApplication(app.conn, backend=backend)
    result = admin.execute("recover", submission_id=operation_id, outcome="applied", reason="restart reconciliation")
    assert result["ok"]
    row = app.conn.execute("SELECT movement_completed_at, destination_movement_attempt_id FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert row["movement_completed_at"] and row["destination_movement_attempt_id"] == attempt_id
    attempt = app.conn.execute("SELECT outcome, purpose FROM movement_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    assert tuple(attempt) == ("confirmed", "destination_submission")


def test_verification_handoff_never_sets_final_movement_marker(tmp_path):
    app, _, operation_id = _prepared(tmp_path)
    row = app.conn.execute("SELECT movement_completed_at, destination_movement_attempt_id FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert row["movement_completed_at"] is None and row["destination_movement_attempt_id"] is None
    movement = app.conn.execute("SELECT purpose, outcome FROM movement_attempts WHERE operation_id=? ORDER BY started_at DESC LIMIT 1", (operation_id,)).fetchone()
    assert tuple(movement) == ("verification_handoff", "confirmed")
