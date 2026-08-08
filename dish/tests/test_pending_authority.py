from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database import (
    confirm_task_content,
    create_operation,
    create_verification_cycle,
    finalize_confirmed_movement_attempt,
    finalize_confirmed_write_attempt,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "upgrade"


def _operation(tmp_path: Path):
    db_path = tmp_path / "dish.sqlite"
    conn = initialize_database(db_path)
    head = confirm_task_content(
        conn, task_gid="task", title="Dish", notes="baseline", schema_version="2"
    )
    op = create_operation(
        conn,
        task_gid="task",
        operation_kind="change",
        expected_identity=head.digest,
        expected_section_gid="research",
        schema_version="2",
        actors=OperationActors(editor_agent="gpt", run_id="run-1"),
        initial_steps={
            "change_intent": {
                "level": "small",
                "reason": "Exercise persistence invariants",
            }
        },
    )
    return db_path, conn, op, head


def test_open_operation_creation_authority_is_immutable(tmp_path: Path) -> None:
    _, conn, op, _ = _operation(tmp_path)
    for sql in (
        "UPDATE operations SET expected_identity='forged' WHERE operation_id=?",
        "UPDATE operations SET expected_section_gid='other' WHERE operation_id=?",
        "UPDATE operations SET task_gid='other' WHERE operation_id=?",
        "DELETE FROM operations WHERE operation_id=?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (op["operation_id"],))


def test_uncertain_operation_blocks_a_second_operation(tmp_path: Path) -> None:
    _, conn, op, head = _operation(tmp_path)
    conn.execute(
        "UPDATE operations SET status='uncertain' WHERE operation_id=?", (op["operation_id"],)
    )
    with pytest.raises(DishRuleError) as exc:
        create_operation(
            conn,
            task_gid="task",
            operation_kind="change",
            expected_identity=head.digest,
            expected_section_gid="research",
            schema_version="2",
        )
    assert exc.value.rule == "open_operation_exists"


def test_pending_external_intent_is_immutable_but_outcome_can_advance(tmp_path: Path) -> None:
    _, conn, op, head = _operation(tmp_path)
    intended = confirm_task_content(
        conn, task_gid="other-task", title="Dish", notes="new", schema_version="2"
    )
    # The separate task above only supplies a truthful digest without advancing task's head.
    conn.execute(
        """INSERT INTO write_attempts(
               attempt_id, operation_id, expected_identity, intended_identity,
               outcome, started_at, purpose, intended_title, intended_notes,
               schema_version
           ) VALUES('write',?,?,?,?,?,'content_write','Dish','new','2')""",
        (op["operation_id"], head.digest, intended.digest, "started", "now"),
    )
    for sql in (
        "UPDATE write_attempts SET intended_notes='forged' WHERE attempt_id='write'",
        "UPDATE write_attempts SET intended_identity='forged' WHERE attempt_id='write'",
        "DELETE FROM write_attempts WHERE attempt_id='write'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.execute("UPDATE write_attempts SET outcome='uncertain' WHERE attempt_id='write'")
    version = finalize_confirmed_write_attempt(
        conn,
        attempt_id="write",
        task_gid="task",
        title="Dish",
        notes="new",
        schema_version="2",
    )
    assert version["identity"] == intended.digest
    assert conn.execute("SELECT outcome FROM write_attempts WHERE attempt_id='write'").fetchone()[0] == "confirmed"

    conn.execute(
        """INSERT INTO movement_attempts(
               attempt_id, operation_id, expected_section_gid, intended_section_gid,
               outcome, started_at, purpose
           ) VALUES('move',?,'research','verification','started','now','verification_handoff')""",
        (op["operation_id"],),
    )
    for sql in (
        "UPDATE movement_attempts SET intended_section_gid='forged' WHERE attempt_id='move'",
        "UPDATE movement_attempts SET purpose='forged' WHERE attempt_id='move'",
        "DELETE FROM movement_attempts WHERE attempt_id='move'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.execute("UPDATE movement_attempts SET outcome='uncertain' WHERE attempt_id='move'")
    finalize_confirmed_movement_attempt(conn, attempt_id="move", live_section_gid="verification")
    assert conn.execute("SELECT outcome FROM movement_attempts WHERE attempt_id='move'").fetchone()[0] == "confirmed"


def test_pending_cycle_and_unused_authorization_are_append_only(tmp_path: Path) -> None:
    _, conn, op, _ = _operation(tmp_path)
    cycle = create_verification_cycle(
        conn,
        operation_id=op["operation_id"],
        task_gid="task",
        cycle_number=1,
        protocol_release="1.0.10",
        protocol_text="protocol",
    )
    for sql, args in (
        ("UPDATE verification_cycles SET cycle_number=2 WHERE cycle_id=?", (cycle["cycle_id"],)),
        ("UPDATE verification_cycles SET protocol_text='other' WHERE cycle_id=?", (cycle["cycle_id"],)),
        ("DELETE FROM verification_cycles WHERE cycle_id=?", (cycle["cycle_id"],)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, args)

    conn.execute(
        """INSERT INTO marco_authorizations(
               authorization_id, task_gid, operation_id, field_name,
               before_json, after_json, reason, actor_run_id, created_at
           ) VALUES('auth','task',?,'Locks','\"a\"','\"b\"','reason','marco','now')""",
        (op["operation_id"],),
    )
    for sql in (
        "UPDATE marco_authorizations SET field_name='Purpose' WHERE authorization_id='auth'",
        "UPDATE marco_authorizations SET after_json='\"c\"' WHERE authorization_id='auth'",
        "DELETE FROM marco_authorizations WHERE authorization_id='auth'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


def test_task_head_requires_exact_confirmed_content_version(tmp_path: Path) -> None:
    db_path, conn, _, _ = _operation(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE task_content_state SET last_confirmed_identity='forged' WHERE task_gid='task'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM task_content_state WHERE task_gid='task'")
    conn.close()
    reopened = initialize_database(db_path)
    assert reopened.execute(
        "SELECT last_confirmed_content_version_id FROM task_content_state WHERE task_gid='task'"
    ).fetchone()[0]
    reopened.close()


@pytest.mark.parametrize(
    "fixture",
    ["dish-tool-recovery-v6.sqlite", "dish-tool-recovery-v17-legacy.sqlite"],
)
def test_historical_recovery_databases_upgrade_with_explicit_reconciliation(
    tmp_path: Path, fixture: str
) -> None:
    path = tmp_path / fixture
    shutil.copy2(FIXTURES / fixture, path)
    conn = initialize_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(
        "SELECT COUNT(*) FROM task_content_state WHERE last_confirmed_content_version_id IS NULL"
    ).fetchone()[0] == 0
    active = conn.execute(
        "SELECT COUNT(*) FROM operations WHERE status IN ('open','uncertain')"
    ).fetchone()[0]
    flagged = conn.execute(
        """SELECT COUNT(*) FROM operations
             WHERE status IN ('open','uncertain')
               AND migration_reconciliation_required=1
               AND migration_reconciliation_reason IS NOT NULL"""
    ).fetchone()[0]
    assert flagged == active
    terminal = conn.execute(
        "SELECT terminal_outcome FROM operations WHERE status='completed'"
    ).fetchall()
    assert all(row[0] for row in terminal)
    conn.close()
