from __future__ import annotations

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend as WorkflowBackend


class Backend(WorkflowBackend):
    def create_bare_task(self, *, title, project_gid, section_gid):
        self.writes += 1
        self.title = title
        self.notes = ""
        self.section = section_gid
        return {"gid": "1000000000000001", "name": title, "notes": ""}


CREATE_REQUEST = "11111111-1111-4111-8111-111111111111"
START_REQUEST = "22222222-2222-4222-8222-222222222222"
VERIFY_REQUEST = "33333333-3333-4333-8333-333333333333"


def _service(tmp_path, backend=None):
    backend = backend or Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    return service, backend


def _principal(run="run"):
    return ServicePrincipal(owner_id="action", run_id=run)


def test_completed_create_request_replays_without_duplicate_task(tmp_path):
    service, backend = _service(tmp_path)
    first = service.execute_agent(
        "create",
        {"agent": "gpt", "title": "Replay dish"},
        principal=_principal(),
        request_id=CREATE_REQUEST,
    )
    second = service.execute_agent(
        "create",
        {"agent": "gpt", "title": "Replay dish"},
        principal=_principal(),
        request_id=CREATE_REQUEST,
    )
    assert first["ok"] and second["ok"]
    assert second["task_gid"] == first["task_gid"]
    assert second["data"]["request_replayed"] is True
    assert backend.writes == 1



def test_completed_start_request_replays_full_stored_result(tmp_path):
    service, _backend = _service(tmp_path)
    first = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    second = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    assert first["ok"] and second["ok"]
    assert second["submission_id"] == first["submission_id"]
    assert second["data"]["request_replayed"] is True
    assert second["data"]["protocol"] == first["data"]["protocol"]
    assert second["data"]["runtime_context"] == first["data"]["runtime_context"]
    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    finally:
        conn.close()

def test_request_id_cannot_be_reused_for_different_work(tmp_path):
    service, backend = _service(tmp_path)
    first = service.execute_agent(
        "create", {"agent": "gpt", "title": "Dish A"},
        principal=_principal(), request_id=CREATE_REQUEST,
    )
    assert first["ok"]
    conflict = service.execute_agent(
        "create", {"agent": "gpt", "title": "Dish B"},
        principal=_principal(), request_id=CREATE_REQUEST,
    )
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"
    assert backend.writes == 1


def test_pending_create_fails_uncertain_instead_of_creating_duplicate(tmp_path):
    service, backend = _service(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn,
            request_id=CREATE_REQUEST,
            owner_id="action",
            run_id="run",
            command="create",
            arguments={"agent": "gpt", "title": "Maybe created"},
        )
    finally:
        conn.close()
    result = service.execute_agent(
        "create", {"agent": "gpt", "title": "Maybe created"},
        principal=_principal(), request_id=CREATE_REQUEST,
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["errors"][0]["rule"] == "service_request_pending"
    assert backend.writes == 0


def test_pending_start_reconciles_exact_existing_operation(tmp_path):
    service, backend = _service(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        prepared = {
            "agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"
        }
        begin_request(
            conn,
            request_id=START_REQUEST,
            owner_id="action",
            run_id="run",
            command="start",
            arguments=prepared,
        )
        app = DishApplication(
            conn,
            backend,
            release_loader=lambda role=None: service._release(role),
            invocation_run_id="run",
        )
        started = app.execute("start", **prepared)
        assert started["ok"]
        operation_id = started["submission_id"]
    finally:
        conn.close()

    replayed = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    assert replayed["ok"]
    assert replayed["submission_id"] == operation_id
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["protocol"]["role"] == "research"
    assert replayed["data"]["protocol"]["text"] == "research protocol"
    assert replayed["data"]["runtime_context"]["verification_queue"]["gid"] == "vq"
    assert replayed["data"]["service_lease"]["run_id"] == "run"



def test_pending_verification_start_reconstructs_exact_review_context(tmp_path):
    service, backend = _service(tmp_path)
    constructor = _principal("constructor-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id=START_REQUEST,
    )
    assert started["ok"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": backend.title + "\n" + backend.notes,
        },
        principal=constructor,
    )
    assert prepared["ok"]

    verifier = _principal("verifier-run")
    request_arguments = {
        "agent": "codex",
        "task_gid": "t",
        "kind": "verification",
        "run_id": "verifier-run",
        "independence_attestation": "independent",
    }
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn,
            request_id=VERIFY_REQUEST,
            owner_id="action",
            run_id="verifier-run",
            command="start",
            arguments=request_arguments,
        )
        app = DishApplication(
            conn,
            backend,
            release_loader=lambda role=None: service._release(role),
            invocation_run_id="verifier-run",
        )
        original = app.execute("start", **request_arguments)
        assert original["ok"]
    finally:
        conn.close()

    replayed = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification", "independence_attestation": "independent"},
        principal=verifier,
        request_id=VERIFY_REQUEST,
    )
    assert replayed["ok"]
    assert replayed["submission_id"] == started["submission_id"]
    assert replayed["allowed_actions"] == ["inspect", "approve", "reject"]
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["reviewed_identity"] == original["data"]["reviewed_identity"]
    assert replayed["data"]["verification_protocol"] == original["data"]["verification_protocol"]
    assert replayed["data"]["task"]["title"] == original["data"]["task"]["title"]
    assert replayed["data"]["service_lease"]["run_id"] == "verifier-run"

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation_id=? "
            "AND event_type='verification.review_started'",
            (started["submission_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()

def test_restore_fault_marker_survives_service_restart(tmp_path):
    service, backend = _service(tmp_path)
    service._restore_fault.set({"kind": "simulated_unproven_restore"})
    restarted = DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )
    health = restarted.health()
    assert not health["ok"]
    assert health["maintenance"]["restore_recovery_required"] is True
    assert health["maintenance"]["restore_fault"]["kind"] == "simulated_unproven_restore"
    blocked = restarted.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    assert blocked["code"] == "INTERNAL_ERROR"
    assert blocked["errors"][0]["rule"] == "service_restore_recovery_required"
    assert backend.writes == 0


def test_action_http_replays_completed_create_with_same_request_id(tmp_path):
    import threading

    from dish_service.client import DishActionClient
    from dish_service.http import build_server

    service, backend = _service(tmp_path)
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        action = DishActionClient(
            f"http://{host}:{port}", token="action-secret", run_id="11111111-2222-4333-8444-555555555555"
        )
        first = action.execute(
            "create",
            {"agent": "gpt", "title": "Replay through HTTP"},
            request_id=CREATE_REQUEST,
        )
        second = action.execute(
            "create",
            {"agent": "gpt", "title": "Replay through HTTP"},
            request_id=CREATE_REQUEST,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert first["ok"] and second["ok"]
    assert second["task_gid"] == first["task_gid"]
    assert second["data"]["request_replayed"] is True
    assert backend.writes == 1


def test_schema_20_upgrades_with_empty_request_ledger(tmp_path):
    import sqlite3

    from dish_tool.constants import SCHEMA_VERSION

    db_path = tmp_path / "v20.sqlite3"
    conn = initialize_database(db_path)
    conn.execute("DROP TABLE service_requests")
    conn.execute("DROP TABLE operation_execution_claims")
    conn.execute("DROP TABLE operation_executions")
    conn.execute("DROP INDEX write_attempts_one_unresolved_operation")
    conn.execute("DROP INDEX movement_attempts_one_unresolved_operation")
    conn.execute("DELETE FROM schema_migrations WHERE version>=21")
    conn.execute("PRAGMA user_version=20")
    conn.close()

    upgraded = initialize_database(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert upgraded.execute(
            "SELECT COUNT(*) FROM service_requests"
        ).fetchone()[0] == 0
        assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.close()


def test_completed_planning_reopen_is_marco_only_audited_and_request_replayed(tmp_path):
    class CompletedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.title = "Bare"
            self.notes = ""
            self.completed = True
            self.reopens = 0

        def read_task(self, gid):
            task = super().read_task(gid)
            task["completed"] = self.completed
            return task

        def update_task_completed(self, *, task_gid, completed):
            self.reopens += 1
            self.completed = completed

    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)
    planner = ServicePrincipal(owner_id="action", run_id="planner-run")
    blocked = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "planning"},
        principal=planner,
        request_id="44444444-4444-4444-8444-444444444444",
    )
    assert blocked["code"] == "WRONG_STATE"
    assert blocked["data"]["required_admin_action"] == "reopen-planning"

    marco = ServicePrincipal(owner_id="admin", run_id="marco-run")
    request_id = "55555555-5555-4555-8555-555555555555"
    first = service.execute_admin(
        "reopen-planning",
        {"task_gid": "t", "reason": "repeat the cook"},
        principal=marco,
        request_id=request_id,
    )
    replayed = service.execute_admin(
        "reopen-planning",
        {"task_gid": "t", "reason": "repeat the cook"},
        principal=marco,
        request_id=request_id,
    )
    assert first["ok"], first
    assert replayed["ok"], replayed
    assert backend.reopens == 1
    assert replayed["data"]["request_replayed"] is True

    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "planning"},
        principal=planner,
        request_id="66666666-6666-4666-8666-666666666666",
    )
    assert started["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        attempt = conn.execute(
            "SELECT request_id,actor_run_id,outcome FROM planning_reopen_attempts WHERE task_gid='t'"
        ).fetchone()
        assert tuple(attempt) == (request_id, "marco-run", "confirmed")
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE task_gid='t' AND event_type='planning.task_reopened'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


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
    assert recovered["data"]["request_id"] == interrupted_request
    assert recovered["data"]["restore_recovered"] is True
    assert sorted(service.config.backup_dir.glob("*pre-restore*.sqlite3")) == (
        pre_restore_before
    )
    assert restarted._restore_requests.read(retry_request) is None


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
