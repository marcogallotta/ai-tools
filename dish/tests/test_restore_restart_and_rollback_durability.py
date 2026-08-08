from __future__ import annotations

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from tests.support.planning_intent import confirmed_planning_start
from dish_service.request_replay import begin_request
from dish_tool.commands import DishApplication
from dish_tool.database_initialization import initialize_database
from dish_tool.database_schema import MIGRATIONS, _execute_script_statements
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend as WorkflowBackend
from tests.support.request_restore import (
    Backend,
    SimulatedSigkill,
    _service,
    principal as _principal,
    restart_service as _restart_service,
    restore_source as _restore_source,
)




CREATE_REQUEST = "11111111-1111-4111-8111-111111111111"
START_REQUEST = "22222222-2222-4222-8222-222222222222"
VERIFY_REQUEST = "33333333-3333-4333-8333-333333333333"










def test_restart_startup_recovers_interrupted_restore_before_health(
    monkeypatch, tmp_path
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "99999999-9999-4999-8999-999999999999"
    original_checkpoint = service._restore_requests.checkpoint

    def checkpoint_then_kill(*, request_id, stage, details):
        checkpoint = original_checkpoint(
            request_id=request_id, stage=stage, details=details
        )
        if stage == "replacement_committed":
            raise SimulatedSigkill(stage)
        return checkpoint

    monkeypatch.setattr(service._restore_requests, "checkpoint", checkpoint_then_kill)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=request_id,
        )

    restarted = _restart_service(service, backend)
    startup = restarted.startup_check()
    assert startup["startup"]["restore_recovery"] == {
        "attempted": True,
        "error_type": None,
        "ok": True,
        "code": "OK",
        "rule": None,
        "request_id": request_id,
    }
    assert startup["maintenance"]["restore_recovery_required"] is False
    assert not restarted._restore_fault.active()

    replayed = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
def test_sigkill_after_pre_restore_snapshot_commit_reuses_exact_snapshot(
    monkeypatch, tmp_path
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    from dish_service.backup import BackupManager

    original_snapshot = BackupManager._snapshot_to
    killed = False

    def snapshot_then_kill(manager, destination):
        nonlocal killed
        record = original_snapshot(manager, destination)
        if not killed and "pre-restore" in destination.name:
            killed = True
            raise SimulatedSigkill("pre-restore-snapshot-committed")
        return record

    monkeypatch.setattr(BackupManager, "_snapshot_to", snapshot_then_kill)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=request_id,
        )
    before = sorted(service.config.backup_dir.glob("*pre-restore*.sqlite3"))
    assert len(before) == 1

    restarted = _restart_service(service, backend)
    recovered = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert recovered["ok"]
    assert recovered["data"]["recovered_from_stage"] == "pre_restore_attempted"
    assert sorted(service.config.backup_dir.glob("*pre-restore*.sqlite3")) == before
def test_sigkill_after_database_swap_before_commit_checkpoint_is_reconciled(
    monkeypatch, tmp_path
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    from dish_service.backup import BackupManager

    original_fsync = BackupManager._fsync_directory
    killed = False

    def fsync_then_kill(path):
        nonlocal killed
        original_fsync(path)
        if not killed and path == service.config.db_path.parent:
            killed = True
            raise SimulatedSigkill("database-swap-committed")

    monkeypatch.setattr(BackupManager, "_fsync_directory", staticmethod(fsync_then_kill))
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=request_id,
        )
    row = service._restore_requests.read(request_id)
    assert service._restore_requests.last_checkpoint(row)["stage"] == (
        "replacement_started"
    )

    restarted = _restart_service(service, backend)
    recovered = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert recovered["ok"]
    assert recovered["data"]["recovered_from_stage"] == "replacement_started"
    assert not restarted._restore_fault.active()
@pytest.mark.parametrize(
    "kill_stage", ["rollback_prepared", "rollback_started", "rolled_back"]
)
def test_sigkill_interrupted_rollback_resumes_exact_candidate(
    monkeypatch, tmp_path, kill_stage
):
    import dish_service.backup as backup_module

    service, backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    original_initialize = backup_module.initialize_database
    original_checkpoint = service._restore_requests.checkpoint

    def fail_only_installed_restore(path):
        conn = original_initialize(path)
        if path == service.config.db_path:
            applied_at = conn.execute(
                "SELECT applied_at FROM schema_migrations "
                "WHERE version=(SELECT MAX(version) FROM schema_migrations)"
            ).fetchone()[0]
            if applied_at != "2999-01-01T00:00:00Z":
                conn.close()
                raise RuntimeError("simulated installed-candidate validation failure")
        return conn

    def checkpoint_then_kill(*, request_id, stage, details):
        checkpoint = original_checkpoint(
            request_id=request_id, stage=stage, details=details
        )
        if stage == kill_stage:
            raise SimulatedSigkill(stage)
        return checkpoint

    monkeypatch.setattr(backup_module, "initialize_database", fail_only_installed_restore)
    monkeypatch.setattr(service._restore_requests, "checkpoint", checkpoint_then_kill)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=request_id,
        )
    assert service._restore_fault.active()

    restarted = _restart_service(service, backend)
    recovered = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert recovered["ok"] is False
    assert recovered["errors"][0]["rule"] == "backup_restore_failed_rolled_back"
    assert recovered["errors"][0]["database_retained"] is True
    assert not restarted._restore_fault.active()

    conn = original_initialize(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT applied_at FROM schema_migrations "
            "WHERE version=(SELECT MAX(version) FROM schema_migrations)"
        ).fetchone()[0] == "2999-01-01T00:00:00Z"
    finally:
        conn.close()
def test_checkpoint_marker_enrichment_failure_does_not_abort_exact_restore(
    monkeypatch, tmp_path
):
    service, _backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    original_set = service._restore_fault.set
    calls = 0

    def set_initial_marker_only(details):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_set(details)
        raise OSError("simulated marker enrichment failure")

    monkeypatch.setattr(service._restore_fault, "set", set_initial_marker_only)
    restored = service.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert restored["ok"]
    assert restored["data"]["request_id"] == request_id
    assert not service._restore_fault.active()
