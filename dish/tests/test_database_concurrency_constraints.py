from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

import dish_tool.database_initialization as database_initialization
import dish_tool.database_migrations as database_migrations
from dish_tool.database_initialization import initialize_database
from dish_tool.database_migrations import _execute_script_statements
from dish_tool.database_schema import MIGRATIONS
from tests.support.thread_teardown import join_thread, managed_thread


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


class _MigrateBeforeBackupConnection(sqlite3.Connection):
    migrate_live_database = None

    def backup(self, target, *args, **kwargs):
        callback = type(self).migrate_live_database
        assert callback is not None
        type(self).migrate_live_database = None
        callback()
        return super().backup(target, *args, **kwargs)


@pytest.mark.database_boundary
@pytest.mark.real_database_bootstrap
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_concurrency
def test_legacy_backup_uses_the_schema_snapshot_that_was_versioned(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "backup-snapshot-v2.sqlite"
    _make_v2(db_path)
    setup = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    finally:
        setup.close()

    tracked_connect = sqlite3.connect
    source_created = False
    migration_ran = False

    def migrate_live_database() -> None:
        nonlocal migration_ran
        migration_ran = True
        writer = tracked_connect(db_path, isolation_level=None)
        try:
            database_migrations.migrate_database(writer)
        finally:
            writer.close()

    def racing_connect(database, *args, **kwargs):
        nonlocal source_created
        if Path(database) == db_path and not source_created:
            source_created = True
            kwargs["factory"] = _MigrateBeforeBackupConnection
            _MigrateBeforeBackupConnection.migrate_live_database = migrate_live_database
        return tracked_connect(database, *args, **kwargs)

    monkeypatch.setattr(database_initialization.sqlite3, "connect", racing_connect)
    initialized = initialize_database(db_path)
    initialized.close()

    assert migration_ran
    backup = sqlite3.connect(db_path.with_suffix(".sqlite.legacy-v2.bak"))
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        backup.close()
    live = sqlite3.connect(db_path)
    try:
        assert live.execute("PRAGMA user_version").fetchone()[0] == max(MIGRATIONS)
    finally:
        live.close()


@pytest.mark.database_boundary
@pytest.mark.real_database_bootstrap
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_concurrency
@pytest.mark.smoke
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

    threads = [managed_thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        join_thread(thread, timeout=15)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == max(MIGRATIONS)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(1, max(MIGRATIONS) + 1))
    finally:
        conn.close()


@pytest.mark.database_boundary
@pytest.mark.real_database_bootstrap
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_concurrency
def test_runtime_schema_validation_uses_one_version_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime-schema-snapshot.sqlite"
    setup = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    finally:
        setup.close()

    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in sorted(MIGRATIONS):
        _execute_script_statements(writer, MIGRATIONS[version])
        writer.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'now')",
            (version,),
        )
        writer.execute(f"PRAGMA user_version = {version}")

    real_connect = sqlite3.connect

    class _CommitMigrationBetweenVersionReads(sqlite3.Connection):
        committed = False

        def execute(self, sql, *args, **kwargs):
            if (
                not type(self).committed
                and "FROM sqlite_master" in sql
                and "schema_migrations" in sql
            ):
                type(self).committed = True
                writer.execute("COMMIT")
            return super().execute(sql, *args, **kwargs)

    def racing_connect(database, *args, **kwargs):
        if Path(database) == db_path:
            kwargs["factory"] = _CommitMigrationBetweenVersionReads
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(database_initialization.sqlite3, "connect", racing_connect)
    runtime = None
    try:
        runtime = database_initialization.open_runtime_database(db_path)
        assert _CommitMigrationBetweenVersionReads.committed
        assert runtime.execute("PRAGMA user_version").fetchone()[0] == max(MIGRATIONS)
        assert runtime.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == max(MIGRATIONS)
    finally:
        if runtime is not None:
            runtime.close()
        if writer.in_transaction:
            writer.execute("ROLLBACK")
        writer.close()


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


@pytest.mark.smoke
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

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                UPDATE movement_attempts
                   SET intended_section_gid='destination', confirmed_section_gid='destination',
                       purpose='destination_submission', outcome='confirmed', finished_at='now'
                 WHERE attempt_id='move'
                """
            )

        conn.execute(
            """
            INSERT INTO movement_attempts (
                attempt_id, operation_id, expected_section_gid,
                intended_section_gid, outcome, started_at, finished_at, purpose,
                confirmed_section_gid
            ) VALUES ('move-destination', 'op', 'verification', 'destination',
                      'confirmed', 'now', 'now', 'destination_submission', 'destination')
            """
        )
        conn.execute(
            "UPDATE operations SET movement_completed_at='now', destination_movement_attempt_id='move-destination' WHERE operation_id='op'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE movement_attempts SET outcome='uncertain' WHERE attempt_id='move-destination'")
    finally:
        conn.close()


@pytest.mark.database_boundary
@pytest.mark.real_database_bootstrap
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_concurrency
@pytest.mark.smoke
def test_many_concurrent_initializers_all_converge(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent-many-v2.sqlite"
    _make_v2(db_path)
    count = 8
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            conn = initialize_database(db_path)
            conn.close()
        except BaseException as exc:
            errors.append(exc)

    threads = [managed_thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        join_thread(thread, timeout=20)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == max(MIGRATIONS)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
