import sqlite3
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

from dish_tool.database import create_operation, mark_operation_completion, confirm_task_content
from dish_tool.database_schema import MIGRATIONS, initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from dish_tool.recovery import begin_movement_attempt, begin_operation_write_attempt, finish_movement_attempt, finish_operation_write_attempt

CURRENT = max(MIGRATIONS)


def test_future_and_disagreeing_schema_claims_fail_closed(tmp_path):
    future = tmp_path / "future.sqlite"
    conn = sqlite3.connect(future)
    conn.execute(f"PRAGMA user_version={CURRENT + 1}")
    conn.commit(); conn.close()
    with pytest.raises(DishRuleError) as exc:
        initialize_database(future)
    assert exc.value.rule == "database_future_user_version"

    disagreement = tmp_path / "disagree.sqlite"
    conn = sqlite3.connect(disagreement)
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES (?, 'now')", (CURRENT - 1,))
    conn.execute(f"PRAGMA user_version={CURRENT}")
    conn.commit(); conn.close()
    with pytest.raises(DishRuleError) as exc:
        initialize_database(disagreement)
    assert exc.value.rule == "database_version_disagreement"


def test_missing_current_table_is_diagnosed(tmp_path):
    path = tmp_path / "missing.sqlite"
    conn = initialize_database(path)
    conn.execute("DROP TABLE marco_authorizations")
    conn.close()
    with pytest.raises(DishRuleError) as exc:
        initialize_database(path)
    assert exc.value.rule == "database_schema_incomplete"
    assert "marco_authorizations" in exc.value.details["missing_tables"]


def test_confirmed_attempts_require_evidence_bindings(tmp_path):
    conn = initialize_database(tmp_path / "db.sqlite")
    confirm_task_content(conn, task_gid="t", title="Old", notes="Notes", schema_version="2")
    expected = conn.execute("SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'").fetchone()[0]
    op = create_operation(conn, task_gid="t", operation_kind="change", expected_identity=expected, schema_version="2", actors=OperationActors(editor_agent="gpt", run_id="r"))
    wa = begin_operation_write_attempt(conn, operation_id=op["operation_id"], expected_identity=expected, intended_identity="new", intended_title="T", intended_notes="N", schema_version="2")
    with pytest.raises(ValueError):
        finish_operation_write_attempt(conn, attempt_id=wa, outcome="confirmed")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE write_attempts SET outcome='confirmed' WHERE attempt_id=?", (wa,))

    ma = begin_movement_attempt(conn, operation_id=op["operation_id"], expected_section_gid="a", intended_section_gid="b", purpose="destination_submission")
    with pytest.raises(ValueError):
        finish_movement_attempt(conn, attempt_id=ma, outcome="confirmed")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE movement_attempts SET outcome='confirmed' WHERE attempt_id=?", (ma,))

    mark_operation_completion(conn, op["operation_id"], "content_write")
    with pytest.raises(sqlite3.IntegrityError):
        mark_operation_completion(conn, op["operation_id"], "signoff")


def test_reader_lock_has_structured_retryable_diagnostic(tmp_path):
    path = tmp_path / "reader.sqlite"
    writer = sqlite3.connect(path)
    writer.execute("CREATE TABLE seed(x INTEGER)")
    writer.execute("INSERT INTO seed VALUES (1)")
    writer.commit()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM seed").fetchall()
    try:
        with pytest.raises(DishRuleError) as exc:
            initialize_database(path)
        assert exc.value.rule == "database_reader_lock"
        assert exc.value.retryable is True
    finally:
        reader.close(); writer.close()
