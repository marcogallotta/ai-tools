from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

from dish_service.leases import ServicePrincipal
from dish_service.maintenance import MaintenanceGate
from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database import initialize_database
from tests.test_dish_tool_r46_operational_hardening import UnavailableBackend, _service


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_startup_remains_available_for_asana_diagnosis(tmp_path):
    service, _backend = _service(tmp_path, backend=UnavailableBackend())

    startup = service.startup_check()

    assert startup["startup_ready"] is True
    assert startup["ok"] is False
    assert startup["asana"]["ok"] is False
    assert startup["asana"]["rule"] == "asana_probe_failed"


def test_startup_remains_available_with_restore_fault_and_invalid_database(tmp_path):
    service, _backend = _service(tmp_path)
    service._restore_fault.set({"kind": "test_restore_fault"})
    service.config.db_path.write_bytes(b"not a sqlite database")

    startup = service.startup_check()

    assert startup["startup_ready"] is True
    assert startup["ok"] is False
    assert startup["database"]["ok"] is False
    assert startup["maintenance"]["restore_recovery_required"] is True
    assert startup["startup"]["database_initialization_error_type"] == "DatabaseError"


def test_corrupt_database_returns_structured_unavailable_results(tmp_path):
    service, _backend = _service(tmp_path)
    service.config.db_path.write_bytes(b"not a sqlite database")
    principal = ServicePrincipal("agent", "run-1")

    agent = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
    )
    admin = service.execute_admin(
        "discard",
        {"submission_id": "11111111-1111-4111-8111-111111111111", "reason": "test"},
        principal=ServicePrincipal("admin", "admin-run"),
    )
    renewal = service.renew_lease(
        "11111111-1111-4111-8111-111111111111", principal
    )
    recovery = service.recover_lease(
        "11111111-1111-4111-8111-111111111111",
        ServicePrincipal("admin", "admin-run"),
        reason="test",
    )
    backup = service.create_backup(label="corrupt")

    for result in (agent, admin, renewal, recovery, backup):
        assert result["code"] == "INTERNAL_ERROR"
        assert result["retryable"] is False
        assert result["errors"][0]["rule"] == "service_database_unavailable"
        assert result["errors"][0]["error_type"] == "DatabaseError"
        assert result["data"]["message"] == (
            "Dish database is unavailable; the request was not executed"
        )


def test_restore_recovers_corrupt_live_database_without_pre_restore_snapshot(tmp_path):
    service, _backend = _service(tmp_path)
    created = service.create_backup(label="known-good")
    assert created["ok"]
    backup_id = created["data"]["backup"]["backup_id"]

    service.config.db_path.write_bytes(b"not a sqlite database")
    for suffix in ("-wal", "-shm"):
        Path(str(service.config.db_path) + suffix).unlink(missing_ok=True)

    restored = service.restore_backup(backup_id)

    assert restored["ok"]
    assert restored["data"]["pre_restore_backup"] is None
    assert restored["data"]["pre_restore_unavailable"]["reason"] == "live_database_not_validated"
    assert restored["data"]["restored_schema_version"] == SCHEMA_VERSION
    assert service.health()["database"]["ok"] is True


def test_restore_migrates_previous_schema_copy_without_mutating_backup(tmp_path):
    service, _backend = _service(tmp_path)
    old_backup = service.config.backup_dir / "dish-schema-20.sqlite3"
    old_backup.parent.mkdir(parents=True, exist_ok=True)

    old_conn = initialize_database(old_backup)
    old_conn.execute("DROP TABLE service_requests")
    old_conn.execute("DROP TABLE operation_execution_claims")
    old_conn.execute("DROP INDEX write_attempts_one_unresolved_operation")
    old_conn.execute("DROP INDEX movement_attempts_one_unresolved_operation")
    old_conn.execute("DELETE FROM schema_migrations WHERE version>=21")
    old_conn.execute("PRAGMA user_version=20")
    old_conn.close()
    before = _digest(old_backup)

    initialize_database(service.config.db_path).close()
    restored = service.restore_backup(old_backup.name)

    assert restored["ok"]
    assert restored["data"]["source_schema_version"] == 20
    assert restored["data"]["restored_schema_version"] == SCHEMA_VERSION
    assert _digest(old_backup) == before
    live = initialize_database(service.config.db_path)
    try:
        assert live.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert live.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0] == 0
    finally:
        live.close()


def test_slow_asana_read_does_not_block_lease_renewal(tmp_path):
    service, backend = _service(tmp_path)
    owner = ServicePrincipal("constructor", "run-1")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run-1"},
        principal=owner,
    )
    assert started["ok"]

    entered = threading.Event()
    release = threading.Event()
    original_read_task = backend.read_task

    def blocked_read(task_gid):
        entered.set()
        assert release.wait(timeout=5)
        return original_read_task(task_gid)

    backend.read_task = blocked_read
    read_result = {}
    read_thread = threading.Thread(
        target=lambda: read_result.update(
            service.execute_agent("read", {"agent": "gpt", "task_gid": "t"}, principal=owner)
        )
    )
    read_thread.start()
    assert entered.wait(timeout=2)

    renewal_result = {}
    renewal_done = threading.Event()

    def renew():
        renewal_result.update(service.renew_lease(started["submission_id"], owner))
        renewal_done.set()

    renewal_thread = threading.Thread(target=renew)
    renewal_thread.start()
    try:
        assert renewal_done.wait(timeout=2), "lease renewal was blocked by unrelated Asana work"
        assert renewal_result["ok"]
    finally:
        release.set()
        read_thread.join(timeout=2)
        renewal_thread.join(timeout=2)
    assert read_result["ok"]


def test_maintenance_gate_gives_waiting_restore_priority():
    gate = MaintenanceGate()
    first_entered = threading.Event()
    release_first = threading.Event()
    restore_entered = threading.Event()
    release_restore = threading.Event()
    second_entered = threading.Event()
    order = []

    def first_request():
        with gate.request():
            order.append("first")
            first_entered.set()
            assert release_first.wait(timeout=5)

    def restore():
        with gate.restore():
            order.append("restore")
            restore_entered.set()
            assert release_restore.wait(timeout=5)

    def second_request():
        with gate.request():
            order.append("second")
            second_entered.set()

    first = threading.Thread(target=first_request)
    writer = threading.Thread(target=restore)
    second = threading.Thread(target=second_request)
    first.start()
    assert first_entered.wait(timeout=2)
    writer.start()
    time.sleep(0.05)
    second.start()
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    assert restore_entered.wait(timeout=2)
    assert not second_entered.is_set()
    release_restore.set()
    first.join(timeout=2)
    writer.join(timeout=2)
    second.join(timeout=2)
    assert order == ["first", "restore", "second"]
