import json
import sqlite3

import pytest

from dish_tool.database import confirm_task_content, create_operation, initialize_database, record_audit
from dish_tool.models import OperationActors


def test_governed_audit_requires_before_after_and_persists_actor_provenance(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    with pytest.raises(ValueError):
        record_audit(conn, submission_id=None, task_gid="t", event_type="x", actor_agent="gpt", details={}, governed_kind="decision")
    event_id = record_audit(
        conn, submission_id=None, task_gid="t", event_type="x", actor_agent="gpt", details={"reason": "review"},
        governed_kind="decision", before_state={"status": "pending-verification"}, after_state={"status": "ready"},
        actor_run_id="run-7", actor_source="verification-command",
    )
    row = conn.execute("SELECT * FROM audit_events WHERE event_id=?", (event_id,)).fetchone()
    assert json.loads(row["before_state"]) == {"status": "pending-verification"}
    assert json.loads(row["after_state"]) == {"status": "ready"}
    assert json.loads(row["actor_provenance"]) == {"agent": "gpt", "independence_attestation": None, "run_id": "run-7", "source": "verification-command"}


def test_operation_lock_audit_has_governed_diff(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = confirm_task_content(conn, task_gid="t", title="title", notes="notes", schema_version="2.0.0")
    op = create_operation(conn, task_gid="t", operation_kind="research", expected_identity=identity.digest, schema_version="2.0.0", actors=OperationActors(editor_agent="gpt", run_id="editor-1"))
    row = conn.execute("SELECT * FROM audit_events WHERE operation_id=? AND governed_kind='lock'", (op["operation_id"],)).fetchone()
    assert json.loads(row["before_state"])["open_operation_id"] is None
    assert json.loads(row["after_state"])["open_operation_id"] == op["operation_id"]
    assert json.loads(row["actor_provenance"])["run_id"] == "editor-1"


def test_database_rejects_impossible_verification_and_operation_combinations(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = confirm_task_content(conn, task_gid="t", title="title", notes="notes", schema_version="2.0.0")
    op = create_operation(conn, task_gid="t", operation_kind="research", expected_identity=identity.digest, schema_version="2.0.0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE operations SET signoff_completed_at='now' WHERE operation_id=?", (op["operation_id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE operations SET status='completed' WHERE operation_id=?", (op["operation_id"],))


def test_governed_exemption_diff_can_record_empty_to_changed(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    event_id = record_audit(
        conn, submission_id=None, task_gid="t", event_type="dish.prepare", actor_agent="gpt", details={},
        governed_kind="exemption", before_state={"tags": [], "revision": None},
        after_state={"tags": ["halal"], "revision": "Marco — approved"},
    )
    row = conn.execute("SELECT governed_kind, before_state, after_state FROM audit_events WHERE event_id=?", (event_id,)).fetchone()
    assert row["governed_kind"] == "exemption"
    assert json.loads(row["before_state"])["tags"] == []
    assert json.loads(row["after_state"])["tags"] == ["halal"]
