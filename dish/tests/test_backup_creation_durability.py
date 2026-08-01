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
        assert creation["status"] == "completed"
        assert creation["sha256"] == replay["data"]["backup"]["sha256"]
        assert creation["size_bytes"] == replay["data"]["backup"]["size_bytes"]
        assert creation["completed_at"] is not None
    finally:
        check.close()
