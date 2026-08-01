from __future__ import annotations

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from tests.planning_intent_support import confirmed_planning_start
from dish_service.request_replay import begin_request
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.database_schema import MIGRATIONS, _execute_script_statements
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend as WorkflowBackend
from tests.support.request_restore import (
    Backend,
    _service,

)




CREATE_REQUEST = "11111111-1111-4111-8111-111111111111"
START_REQUEST = "22222222-2222-4222-8222-222222222222"
VERIFY_REQUEST = "33333333-3333-4333-8333-333333333333"




def _principal(run="run"):
    return ServicePrincipal(owner_id="action", run_id=run)

class SimulatedSigkill(BaseException):
    pass

def _restore_source(service):
    initialize_database(service.config.db_path).close()
    source = service.backup_manager.create(label="sigkill-source")
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            "UPDATE schema_migrations SET applied_at='2999-01-01T00:00:00Z' "
            "WHERE version=(SELECT MAX(version) FROM schema_migrations)"
        )
    finally:
        conn.close()
    return source

def _restart_service(service, backend):
    return DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )



@pytest.mark.parametrize(
    "kill_stage",
    [
        "preparation_started",
        "candidate_prepared",
        "pre_restore_attempted",
        "pre_restore_captured",
        "replacement_started",
        "replacement_committed",
        "validated",
    ],
)
def test_sigkill_interrupted_restore_recovers_from_exact_checkpoint(
    monkeypatch, tmp_path, kill_stage
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "44444444-4444-4444-8444-444444444444"
    original_checkpoint = service._restore_requests.checkpoint

    def checkpoint_then_kill(*, request_id, stage, details):
        checkpoint = original_checkpoint(
            request_id=request_id, stage=stage, details=details
        )
        if stage == kill_stage:
            raise SimulatedSigkill(stage)
        return checkpoint

    monkeypatch.setattr(service._restore_requests, "checkpoint", checkpoint_then_kill)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=request_id,
        )

    pre_restore_before = sorted(
        service.config.backup_dir.glob("*pre-restore*.sqlite3")
    )
    assert service._restore_fault.active()

    restarted = _restart_service(service, backend)
    recovered = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert recovered["ok"]
    assert recovered["data"]["restore_recovered"] is True
    assert recovered["data"]["request_id"] == request_id
    assert (
        recovered["data"]["restored"]["source_backup_id"]
        == source.backup_id
    )
    assert not restarted._restore_fault.active()
    pre_restore_after = sorted(
        service.config.backup_dir.glob("*pre-restore*.sqlite3")
    )
    if kill_stage in {"preparation_started", "candidate_prepared", "pre_restore_attempted"}:
        assert len(pre_restore_before) == 0
        assert len(pre_restore_after) == 1
    else:
        assert len(pre_restore_before) == 1
        assert pre_restore_after == pre_restore_before

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT applied_at FROM schema_migrations "
            "WHERE version=(SELECT MAX(version) FROM schema_migrations)"
        ).fetchone()[0] != "2999-01-01T00:00:00Z"
    finally:
        conn.close()

    replayed = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
def test_restore_recovers_when_killed_after_request_acceptance_before_marker(
    monkeypatch, tmp_path
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "66666666-6666-4666-8666-666666666666"

    def kill_before_marker(details):
        raise SimulatedSigkill("request-accepted")

    monkeypatch.setattr(service._restore_fault, "set", kill_before_marker)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=request_id,
        )
    assert not service._restore_fault.active()

    restarted = _restart_service(service, backend)
    recovered = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert recovered["ok"]
    assert recovered["data"]["restore_recovered"] is True
    assert recovered["data"]["recovered_from_stage"] == "request_accepted"
    assert not restarted._restore_fault.active()
def test_new_client_request_returns_recovered_restore_without_second_swap(
    monkeypatch, tmp_path
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    interrupted_request = "77777777-7777-4777-8777-777777777777"
    retry_request = "88888888-8888-4888-8888-888888888888"
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
            request_id=interrupted_request,
        )
    pre_restore_before = sorted(
        service.config.backup_dir.glob("*pre-restore*.sqlite3")
    )

    restarted = _restart_service(service, backend)
    recovered = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=retry_request,
    )
    assert recovered["ok"]
    assert recovered["data"]["request_id"] == retry_request
    assert recovered["data"]["recovered_request_id"] == interrupted_request
    assert recovered["data"]["request_replayed"] is True
    assert recovered["data"]["restore_recovered"] is True
    assert sorted(service.config.backup_dir.glob("*pre-restore*.sqlite3")) == (
        pre_restore_before
    )
    alias = restarted._restore_requests.read(retry_request)
    assert alias is not None
    assert alias["replay_of_request_id"] == interrupted_request

    replayed = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=retry_request,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_id"] == retry_request
    assert replayed["data"]["recovered_request_id"] == interrupted_request
    assert replayed["data"]["request_replayed"] is True
    assert sorted(service.config.backup_dir.glob("*pre-restore*.sqlite3")) == (
        pre_restore_before
    )
def test_fresh_request_recovers_request_accepted_before_marker_without_duplicate(
    monkeypatch, tmp_path
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    interrupted_request = "12121212-1212-4212-8212-121212121212"
    retry_request = "13131313-1313-4313-8313-131313131313"

    def kill_before_marker(_details):
        raise SimulatedSigkill("request-accepted")

    monkeypatch.setattr(service._restore_fault, "set", kill_before_marker)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=interrupted_request,
        )
    assert not service._restore_fault.active()
    assert service._restore_requests.last_checkpoint(
        service._restore_requests.read(interrupted_request)
    )["stage"] == "request_accepted"

    restarted = _restart_service(service, backend)
    recovered = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=retry_request,
    )
    assert recovered["ok"]
    assert recovered["data"]["request_id"] == retry_request
    assert recovered["data"]["recovered_request_id"] == interrupted_request
    assert recovered["data"]["restore_recovered"] is True
    assert recovered["data"]["recovered_from_stage"] == "request_accepted"
    assert recovered["data"]["request_replayed"] is True
    assert not restarted._restore_fault.active()
    assert restarted._restore_requests.read(retry_request)[
        "replay_of_request_id"
    ] == interrupted_request
def test_fresh_request_after_startup_recovery_replays_without_second_restore(
    monkeypatch, tmp_path
):
    from dish_service.backup import BackupManager

    service, backend = _service(tmp_path)
    source = _restore_source(service)
    interrupted_request = "14141414-1414-4414-8414-141414141414"
    retry_request = "15151515-1515-4515-8515-151515151515"
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
            request_id=interrupted_request,
        )

    restarted = _restart_service(service, backend)
    startup = restarted.startup_check()
    assert startup["startup"]["restore_recovery"]["ok"] is True
    assert startup["startup"]["restore_recovery"]["request_id"] == interrupted_request
    assert not restarted._restore_fault.active()
    pre_restore_before = sorted(
        service.config.backup_dir.glob("*pre-restore*.sqlite3")
    )

    def forbidden_restore(self, _backup_id):
        raise AssertionError("restore executed again after startup recovery")

    def forbidden_recover(self, _backup_id, _checkpoint):
        raise AssertionError("restore recovery executed again")

    monkeypatch.setattr(BackupManager, "restore", forbidden_restore)
    monkeypatch.setattr(BackupManager, "recover_restore", forbidden_recover)
    replayed = restarted.restore_backup(
        source.backup_id,
        principal=_principal("fresh-run"),
        request_id=retry_request,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_id"] == retry_request
    assert replayed["data"]["recovered_request_id"] == interrupted_request
    assert replayed["data"]["request_replayed"] is True
    assert sorted(service.config.backup_dir.glob("*pre-restore*.sqlite3")) == (
        pre_restore_before
    )
    assert restarted._restore_requests.read(retry_request)[
        "replay_of_request_id"
    ] == interrupted_request
def test_completed_restore_retry_clears_stale_in_progress_marker(
    monkeypatch, tmp_path
):
    service, backend = _service(tmp_path)
    source = _restore_source(service)
    request_id = "55555555-5555-4555-8555-555555555555"

    def kill_instead_of_clear():
        raise SimulatedSigkill("after-journal-complete")

    monkeypatch.setattr(service._restore_fault, "clear", kill_instead_of_clear)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=request_id,
        )
    assert service._restore_fault.active()

    restarted = _restart_service(service, backend)
    replayed = restarted.restore_backup(
        source.backup_id,
        principal=_principal("restore-run"),
        request_id=request_id,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
    assert not restarted._restore_fault.active()
def test_fresh_request_after_terminal_result_marker_recovery_does_not_restore_again(
    monkeypatch, tmp_path
):
    from dish_service.backup import BackupManager

    service, backend = _service(tmp_path)
    source = _restore_source(service)
    interrupted_request = "56565656-5656-4656-8656-565656565656"
    retry_request = "57575757-5757-4757-8757-575757575757"

    def kill_instead_of_clear():
        raise SimulatedSigkill("after-journal-complete")

    monkeypatch.setattr(service._restore_fault, "clear", kill_instead_of_clear)
    with pytest.raises(SimulatedSigkill):
        service.restore_backup(
            source.backup_id,
            principal=_principal("restore-run"),
            request_id=interrupted_request,
        )

    restarted = _restart_service(service, backend)
    startup = restarted.startup_check()
    assert startup["startup"]["restore_recovery"]["ok"] is True
    assert startup["startup"]["restore_recovery"]["request_id"] == interrupted_request
    source_row = restarted._restore_requests.read(interrupted_request)
    assert source_row["recovered_from_interruption"] is True
    assert not restarted._restore_fault.active()

    def forbidden_restore(self, _backup_id):
        raise AssertionError("restore executed again after terminal-result recovery")

    def forbidden_recover(self, _backup_id, _checkpoint):
        raise AssertionError("restore recovery executed again")

    monkeypatch.setattr(BackupManager, "restore", forbidden_restore)
    monkeypatch.setattr(BackupManager, "recover_restore", forbidden_recover)
    replayed = restarted.restore_backup(
        source.backup_id,
        principal=_principal("fresh-run"),
        request_id=retry_request,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_id"] == retry_request
    assert replayed["data"]["recovered_request_id"] == interrupted_request
    assert replayed["data"]["request_replayed"] is True
    assert restarted._restore_requests.read(retry_request)[
        "replay_of_request_id"
    ] == interrupted_request
