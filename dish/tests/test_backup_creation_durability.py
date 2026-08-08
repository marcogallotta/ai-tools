from __future__ import annotations

import pytest
import sqlite3

import dish_service.application as application_module
from dish_service.backup import BackupManager
from dish_service.leases import ServicePrincipal
from dish_tool.database_initialization import initialize_database
from tests.support.operational import _service


REQUEST_ID = "28000000-0000-4000-8000-000000000028"
RUN_ID = "28000000-0000-4000-8000-000000000029"


def _principal() -> ServicePrincipal:
    return ServicePrincipal(owner_id="admin", run_id=RUN_ID)


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_durability
def test_backup_identity_is_durable_before_snapshot_creation(monkeypatch, tmp_path):
    service, _backend = _service(tmp_path)
    real_create = BackupManager.create
    observed = {}

    def inspect_reservation(manager, *, label="manual", backup_id=None):
        conn = initialize_database(service.config.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
            ).fetchone()
            observed.update(dict(row) if row is not None else {})
        finally:
            conn.close()
        return real_create(manager, label=label, backup_id=backup_id)

    monkeypatch.setattr(BackupManager, "create", inspect_reservation)
    result = service.create_backup(
        label="identity-first", principal=_principal(), request_id=REQUEST_ID
    )

    assert result["ok"] is True
    assert observed["status"] == "reserved"
    assert observed["backup_id"] == result["data"]["backup"]["backup_id"]
    assert observed["sha256"] is None
    assert observed["completed_at"] is None


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_durability
def test_interrupted_result_persistence_reconciles_exact_reserved_backup(
    monkeypatch, tmp_path
):
    service, _backend = _service(tmp_path)
    real_complete = application_module.complete_request

    def interrupted_completion(*args, **kwargs):
        raise sqlite3.OperationalError("simulated result persistence interruption")

    monkeypatch.setattr(application_module, "complete_request", interrupted_completion)
    first = service.create_backup(
        label="reconcile", principal=_principal(), request_id=REQUEST_ID
    )
    assert first["ok"] is False
    assert first["code"] == "INTERNAL_ERROR"

    conn = initialize_database(service.config.db_path)
    try:
        request = conn.execute(
            "SELECT status,result_json FROM service_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()
        creation = conn.execute(
            "SELECT * FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()
        assert request["status"] == "pending"
        assert request["result_json"] is None
        assert creation["status"] == "reserved"
        assert creation["sha256"] is None
        reserved_id = creation["backup_id"]
    finally:
        conn.close()

    backups = list(service.config.backup_dir.glob("*.sqlite3"))
    assert [path.name for path in backups] == [reserved_id]

    monkeypatch.setattr(application_module, "complete_request", real_complete)
    replay = service.create_backup(
        label="reconcile", principal=_principal(), request_id=REQUEST_ID
    )
    assert replay["ok"] is True
    assert replay["data"]["backup"]["backup_id"] == reserved_id
    assert replay["data"]["request_replayed"] is True
    assert replay["data"]["backup_recovered_from_interruption"] is True
    assert [path.name for path in service.config.backup_dir.glob("*.sqlite3")] == [
        reserved_id
    ]

    check = initialize_database(service.config.db_path)
    try:
        request = check.execute(
            "SELECT status,result_json FROM service_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()
        creation = check.execute(
            "SELECT * FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()
        assert request["status"] == "completed"
        assert request["result_json"] is not None
        assert creation["status"] == "confirmed"
        assert creation["sha256"] == replay["data"]["backup"]["sha256"]
        assert creation["size_bytes"] == replay["data"]["backup"]["size_bytes"]
        assert creation["completed_at"] is not None
    finally:
        check.close()


def test_pre_rename_failure_is_durably_not_applied(monkeypatch, tmp_path):
    import dish_service.backup as backup_module

    service, _backend = _service(tmp_path)
    real_replace = backup_module.os.replace

    def fail_backup_replace(source, destination):
        if destination.parent == service.config.backup_dir:
            raise PermissionError("pre-rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", fail_backup_replace)
    result = service.create_backup(
        label="pre-rename", principal=_principal(), request_id=REQUEST_ID
    )
    assert result["code"] == "BACKEND_REJECTED"
    assert result["errors"][0]["backup_creation_outcome"] == "not_applied"

    conn = initialize_database(service.config.db_path)
    try:
        creation = conn.execute(
            "SELECT * FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()
    finally:
        conn.close()
    assert creation["status"] == "not_applied"
    assert not (service.config.backup_dir / creation["backup_id"]).exists()


def test_post_rename_fsync_failure_stays_uncertain_until_exact_replay(
    monkeypatch, tmp_path
):
    service, _backend = _service(tmp_path)

    with monkeypatch.context() as failed_fsync:
        failed_fsync.setattr(
            BackupManager,
            "_fsync_directory",
            staticmethod(
                lambda _path: (_ for _ in ()).throw(OSError("fsync failed"))
            ),
        )
        first = service.create_backup(
            label="post-rename", principal=_principal(), request_id=REQUEST_ID
        )

    assert first["code"] == "BACKEND_UNCERTAIN"
    conn = initialize_database(service.config.db_path)
    try:
        creation = conn.execute(
            "SELECT * FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()
    finally:
        conn.close()
    assert creation["status"] == "uncertain"
    assert (service.config.backup_dir / creation["backup_id"]).is_file()

    replay = service.create_backup(
        label="post-rename", principal=_principal(), request_id=REQUEST_ID
    )
    assert replay["ok"] is True
    assert replay["data"]["backup"]["backup_id"] == creation["backup_id"]
    check = initialize_database(service.config.db_path)
    try:
        assert check.execute(
            "SELECT status FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()[0] == "confirmed"
    finally:
        check.close()


def test_replay_reconciles_reserved_destination_before_previous_failure(tmp_path):
    from dish_service.backup_creation_journal import reserve_backup_creation
    from dish_service.request_replay import begin_request, complete_request
    from dish_tool.errors import DishRuleError
    from dish_tool.results import error_envelope

    service, _backend = _service(tmp_path)
    backup_id = service.backup_manager.new_backup_id(label="prior-failure")
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn, request_id=REQUEST_ID, owner_id=_principal().owner_id,
            run_id=_principal().run_id, command="backup-create",
            arguments={"label": "prior-failure"},
        )
        reserve_backup_creation(
            conn, request_id=REQUEST_ID, backup_id=backup_id
        )
        complete_request(
            conn, request_id=REQUEST_ID,
            result=error_envelope(
                "backup-create",
                DishRuleError(
                    "BACKEND_REJECTED", "historical pre-fix failure",
                    rule="historical_backup_failure",
                ),
            ),
        )
    finally:
        conn.close()

    service.backup_manager.create(label="prior-failure", backup_id=backup_id)
    replay = service.create_backup(
        label="prior-failure", principal=_principal(), request_id=REQUEST_ID
    )
    assert replay["ok"] is True
    assert replay["data"]["backup"]["backup_id"] == backup_id
    assert replay["data"]["request_replayed"] is True

    check = initialize_database(service.config.db_path)
    try:
        request = check.execute(
            "SELECT resolution_result_json FROM service_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()
        creation = check.execute(
            "SELECT status FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()
    finally:
        check.close()
    assert request["resolution_result_json"] is not None
    assert creation["status"] == "confirmed"


def test_startup_closes_existing_absent_reservation(tmp_path):
    from dish_service.backup_creation_journal import reserve_backup_creation
    from dish_service.request_replay import begin_request

    service, _backend = _service(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn, request_id=REQUEST_ID, owner_id=_principal().owner_id,
            run_id=_principal().run_id, command="backup-create",
            arguments={"label": "orphan"},
        )
        reserve_backup_creation(
            conn, request_id=REQUEST_ID,
            backup_id=service.backup_manager.new_backup_id(label="orphan"),
        )
    finally:
        conn.close()

    startup = service.startup_check()["startup"]["backup_creation_recovery"]
    assert startup["discovered"] == 1
    assert startup["not_applied"] == 1
    assert startup["uncertain"] == 0

    check = initialize_database(service.config.db_path)
    try:
        creation = check.execute(
            "SELECT status FROM backup_creations WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()[0]
        request = check.execute(
            "SELECT status FROM service_requests WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()[0]
    finally:
        check.close()
    assert creation == "not_applied"
    assert request == "completed"


def test_confirmed_reservation_is_valid_before_request_resolution(tmp_path):
    from dish_service.backup_creation_journal import (
        finish_backup_creation,
        reserve_backup_creation,
    )
    from dish_service.request_replay import begin_request
    from dish_tool.transactions import immediate_transaction

    service, _backend = _service(tmp_path)
    backup_id = service.backup_manager.new_backup_id(label="confirmed-frontier")
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn, request_id=REQUEST_ID, owner_id=_principal().owner_id,
            run_id=_principal().run_id, command="backup-create",
            arguments={"label": "confirmed-frontier"},
        )
        reserve_backup_creation(conn, request_id=REQUEST_ID, backup_id=backup_id)
    finally:
        conn.close()

    record = service.backup_manager.create(
        label="confirmed-frontier", backup_id=backup_id
    )
    conn = initialize_database(service.config.db_path)
    try:
        with immediate_transaction(conn, "test_confirmed_frontier"):
            finish_backup_creation(
                conn, request_id=REQUEST_ID, outcome="confirmed",
                reason="simulated_crash_after_reconciliation", record=record,
            )
    finally:
        conn.close()

    # The confirmed filesystem fact is a supported recovery frontier even though
    # the request result has not yet been made durable. Startup closes that exact
    # request without allocating or snapshotting another destination.
    reopened = initialize_database(service.config.db_path)
    reopened.close()
    startup = service.startup_check()["startup"]["backup_creation_recovery"]
    assert startup["confirmed"] == 1
    assert startup["uncertain"] == 0

    replay = service.create_backup(
        label="confirmed-frontier", principal=_principal(), request_id=REQUEST_ID
    )
    assert replay["ok"] is True
    assert replay["data"]["backup"]["backup_id"] == backup_id


def test_successful_backup_replay_keeps_original_result_authoritative(tmp_path):
    service, _backend = _service(tmp_path)
    first = service.create_backup(
        label="stable-success", principal=_principal(), request_id=REQUEST_ID
    )
    assert first["ok"] is True

    replay = service.create_backup(
        label="stable-success", principal=_principal(), request_id=REQUEST_ID
    )
    assert replay["ok"] is True
    assert replay["data"]["backup"] == first["data"]["backup"]

    conn = initialize_database(service.config.db_path)
    try:
        request = conn.execute(
            "SELECT result_json,resolution_result_json FROM service_requests "
            "WHERE request_id=?", (REQUEST_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert request["result_json"] is not None
    assert request["resolution_result_json"] is None


def test_startup_closes_terminal_not_applied_reservation_with_pending_request(tmp_path):
    from dish_service.backup_creation_journal import (
        finish_backup_creation,
        reserve_backup_creation,
    )
    from dish_service.request_replay import begin_request
    from dish_tool.transactions import immediate_transaction

    service, _backend = _service(tmp_path)
    backup_id = service.backup_manager.new_backup_id(label="not-applied-frontier")
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn, request_id=REQUEST_ID, owner_id=_principal().owner_id,
            run_id=_principal().run_id, command="backup-create",
            arguments={"label": "not-applied-frontier"},
        )
        reserve_backup_creation(conn, request_id=REQUEST_ID, backup_id=backup_id)
        with immediate_transaction(conn, "test_not_applied_frontier"):
            finish_backup_creation(
                conn, request_id=REQUEST_ID, outcome="not_applied",
                reason="simulated_crash_before_request_completion",
            )
    finally:
        conn.close()

    startup = service.startup_check()["startup"]["backup_creation_recovery"]
    assert startup["not_applied"] == 1
    assert startup["uncertain"] == 0

    check = initialize_database(service.config.db_path)
    try:
        request = check.execute(
            "SELECT status,result_json FROM service_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()
    finally:
        check.close()
    assert request["status"] == "completed"
    assert request["result_json"] is not None


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_upgrade
def test_schema_39_migrates_backup_reservations_and_completed_rows(tmp_path):
    from dish_service.request_replay import begin_request, complete_request
    from dish_tool.database_migrations import _execute_script_statements
    from dish_tool.database_schema import MIGRATIONS
    from dish_tool.results import result_envelope

    db_path = tmp_path / "schema-38.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 39):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version={version}")
        conn.execute("COMMIT")

        completed_request = "28000000-0000-4000-8000-000000000030"
        reserved_request = "28000000-0000-4000-8000-000000000031"
        completed_backup = "dish-schema38-completed.sqlite3"
        reserved_backup = "dish-schema38-reserved.sqlite3"
        begin_request(
            conn, request_id=completed_request, owner_id="admin", run_id=RUN_ID,
            command="backup-create", arguments={"label": "completed"},
        )
        begin_request(
            conn, request_id=reserved_request, owner_id="admin", run_id=RUN_ID,
            command="backup-create", arguments={"label": "reserved"},
        )
        conn.execute(
            "INSERT INTO backup_creations(request_id,backup_id,status,created_at) "
            "VALUES(?,?,'reserved','before')",
            (completed_request, completed_backup),
        )
        conn.execute(
            "INSERT INTO backup_creations(request_id,backup_id,status,created_at) "
            "VALUES(?,?,'reserved','before')",
            (reserved_request, reserved_backup),
        )
        conn.execute(
            "UPDATE backup_creations SET status='completed',sha256=?,size_bytes=7,"
            "completed_at='after' WHERE request_id=?",
            ("a" * 64, completed_request),
        )
        complete_request(
            conn, request_id=completed_request,
            result=result_envelope(
                command="backup-create",
                data={"backup": {
                    "backup_id": completed_backup,
                    "sha256": "a" * 64,
                    "size_bytes": 7,
                }},
            ),
        )
    finally:
        conn.close()

    upgraded = initialize_database(db_path)
    try:
        rows = {
            row["request_id"]: row
            for row in upgraded.execute(
                "SELECT * FROM backup_creations ORDER BY request_id"
            )
        }
        assert rows[completed_request]["status"] == "confirmed"
        assert rows[completed_request]["resolution_reason"] == "migrated_confirmed"
        assert rows[reserved_request]["status"] == "reserved"
        assert rows[reserved_request]["resolution_reason"] is None
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 40
    finally:
        upgraded.close()
