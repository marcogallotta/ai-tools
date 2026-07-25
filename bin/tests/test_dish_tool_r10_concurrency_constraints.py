from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from dish_tool.database_schema import (
    MIGRATIONS,
    _execute_script_statements,
    initialize_database,
)


def _make_v2(path: Path) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in (1, 2):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_concurrent_initializers_serialize_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent-v2.sqlite"
    _make_v2(db_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            conn = initialize_database(db_path)
            conn.close()
        except BaseException as exc:  # surfaced below with both thread outcomes
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == max(MIGRATIONS)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(1, max(MIGRATIONS) + 1))
    finally:
        conn.close()


def _base_operation(conn: sqlite3.Connection, operation_id: str = "op") -> None:
    conn.execute(
        """
        INSERT INTO operations (
            operation_id, task_gid, operation_kind, status, expected_identity,
            schema_version, created_at
        ) VALUES (?, 'task', 'change', 'open', 'expected', '2', 'now')
        """,
        (operation_id,),
    )


def _content_version(conn: sqlite3.Connection, version_id: str, identity: str = "identity") -> None:
    conn.execute(
        """
        INSERT INTO content_versions (
            content_version_id, task_gid, operation_id, boundary, identity,
            title, notes, confirmed, created_at
        ) VALUES (?, 'task', 'op', 'reviewed', ?, 'Title', 'Notes', 1, 'now')
        """,
        (version_id, identity),
    )


def test_database_rejects_stronger_impossible_states(tmp_path: Path) -> None:
    conn = initialize_database(tmp_path / "constraints.sqlite")
    try:
        _base_operation(conn)
        _content_version(conn, "cv")

        bad_cycles = [
            # reviewed binding is unpaired
            ("c1", "cv", None, None, None, None, None, None),
            # reviewed identity does not match the confirmed content version
            ("c2", "cv", "wrong", None, None, None, None, None),
            # signed binding is unpaired
            ("c3", "cv", "identity", "cv", None, None, None, None),
            # approved cycle is not complete and signed
            ("c4", "cv", "identity", None, None, "approved", None, None),
            # route requires a resume state
            ("c5", "cv", "identity", None, None, None, "evidence", None),
        ]
        for row in bad_cycles:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO verification_cycles (
                        cycle_id, operation_id, task_gid, cycle_number,
                        protocol_release, reviewed_content_version_id,
                        reviewed_identity, signed_content_version_id,
                        signed_identity, outcome, route, resume_state, created_at
                    ) VALUES (?, 'op', 'task',
                              (SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid='task'),
                              '1', ?, ?, ?, ?, ?, ?, ?, 'now')
                    """,
                    row,
                )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO operations (
                    operation_id, task_gid, operation_kind, status,
                    expected_identity, schema_version, signoff_completed_at, created_at
                ) VALUES ('bad-signoff', 'task-2', 'change', 'open', 'x', '2', 'now', 'now')
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO operations (
                    operation_id, task_gid, operation_kind, status,
                    expected_identity, schema_version, created_at
                ) VALUES ('bad-complete', 'task-3', 'change', 'completed', 'x', '2', 'now')
                """
            )

        conn.execute(
            """
            INSERT INTO movement_attempts (
                attempt_id, operation_id, expected_section_gid,
                intended_section_gid, outcome, started_at, purpose,
                confirmed_section_gid
            ) VALUES ('move', 'op', 'research', 'verification', 'uncertain', 'now',
                      'verification_handoff', 'verification')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE operations SET movement_completed_at='now', destination_movement_attempt_id='move' WHERE operation_id='op'"
            )

        conn.execute(
            """
            UPDATE movement_attempts
               SET intended_section_gid='destination', confirmed_section_gid='destination',
                   purpose='destination_submission', outcome='confirmed'
             WHERE attempt_id='move'
            """
        )
        conn.execute(
            "UPDATE operations SET movement_completed_at='now', destination_movement_attempt_id='move' WHERE operation_id='op'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE movement_attempts SET outcome='uncertain' WHERE attempt_id='move'")
    finally:
        conn.close()
