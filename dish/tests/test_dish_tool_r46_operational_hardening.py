from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from dish_service.application import DishService
from dish_service.client import DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database import initialize_database, record_command_audit_repair
from dish_tool.errors import DishRuleError
from dish_tool.results import result_envelope
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend, TASK


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


class UnavailableBackend(Backend):
    def list_sections(self, project_gid):
        raise DishRuleError(
            "BACKEND_REJECTED",
            "Asana health probe failed",
            rule="asana_probe_failed",
            retryable=True,
        )


def _service(tmp_path, backend=None, *, clock=None, ttl=90):
    backend = backend or Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "managed-backups",
            lease_ttl_seconds=ttl,
            agent_token="agent-secret",
            admin_token="admin-secret",
            port=0,
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
        lease_now=None if clock is None else clock.now,
    )
    return service, backend


def _approved(service: DishService):
    constructor = ServicePrincipal("constructor", "constructor-run")
    verifier = ServicePrincipal("verifier", "verifier-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "constructor-run"},
        principal=constructor,
    )
    assert started["ok"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=constructor,
    )
    assert prepared["ok"]
    review = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification", "run_id": "verifier-run", "independence_attestation": "independent"},
        principal=verifier,
    )
    assert review["ok"]
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": started["submission_id"]},
        principal=verifier,
    )
    assert inspected["ok"]
    approved = service.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "correction": "none",
            "reviewed_identity": review["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
            "run_id": "verifier-run",
        },
        principal=verifier,
    )
    assert approved["ok"]
    return started["submission_id"], verifier


def test_backup_restore_preserves_open_signoff_lease_and_recovery_state(tmp_path):
    service, _backend = _service(tmp_path)
    operation_id, verifier = _approved(service)

    conn = initialize_database(service.config.db_path)
    try:
        repair_id = record_command_audit_repair(
            conn,
            command="approve",
            result=result_envelope(command="approve", submission_id=operation_id),
            audit_error="simulated missing final audit",
            operation_id=operation_id,
            submission_id=None,
            task_gid="t",
            actor_agent="codex",
        )
    finally:
        conn.close()

    created = service.create_backup(label="await-submission")
    assert created["ok"]
    backup_id = created["data"]["backup"]["backup_id"]

    submitted = service.execute_agent("submit", {"submission_id": operation_id}, principal=verifier)
    assert submitted["ok"]

    restored = service.restore_backup(backup_id)
    assert restored["ok"]
    assert restored["data"]["pre_restore_backup"]["backup_id"] != backup_id

    conn = initialize_database(service.config.db_path)
    try:
        op = conn.execute(
            "SELECT status,phase,signoff_completed_at,completed_at FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        lease = conn.execute(
            "SELECT owner_id,run_id,released_at FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
        cycle = conn.execute(
            "SELECT outcome,signed_identity,signed_content_version_id FROM verification_cycles WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        repair = conn.execute(
            "SELECT repaired_at FROM command_audit_repairs WHERE repair_id=?", (repair_id,)
        ).fetchone()
    finally:
        conn.close()

    assert tuple(op[:2]) == ("open", "await_submission")
    assert op["signoff_completed_at"] and op["completed_at"] is None
    assert tuple(lease) == ("verifier", "verifier-run", None)
    assert cycle["outcome"] == "approved"
    assert cycle["signed_identity"] and cycle["signed_content_version_id"]
    assert repair["repaired_at"] is None


def test_restore_rejects_unmanaged_or_traversal_backup_identifier(tmp_path):
    service, _backend = _service(tmp_path)
    result = service.restore_backup("../outside.sqlite3")
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "backup_id_invalid"


def test_startup_processes_pending_audit_repairs_and_reports_health(tmp_path):
    service, _backend = _service(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        repair_id = record_command_audit_repair(
            conn,
            command="read",
            result=result_envelope(command="read"),
            audit_error="simulated",
        )
    finally:
        conn.close()

    startup = service.startup_check()
    assert startup["ok"]
    assert startup["startup"]["audit_repairs_processed"] == 1
    assert startup["database"]["schema_version"] == SCHEMA_VERSION
    assert startup["audit"]["pending_repairs"] == 0
    assert startup["asana"]["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT repaired_at FROM command_audit_repairs WHERE repair_id=?", (repair_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_asana_health_failure_blocks_mutation_before_any_effect(tmp_path):
    backend = UnavailableBackend()
    service, _backend = _service(tmp_path, backend)
    health = service.health()
    result = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"}
    )
    assert not health["ok"]
    assert not health["asana"]["ok"]
    assert result["code"] == "BACKEND_REJECTED"
    assert result["errors"][0]["rule"] == "asana_probe_failed"
    assert backend.writes == 0
    assert backend.moves == 0
    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    finally:
        conn.close()


def test_admin_recovery_runs_through_service_after_explicit_stale_lease_recovery(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=30)
    operation_id, _verifier = _approved(service)
    clock.advance(31)
    admin = ServicePrincipal("marco-admin", "recovery-run")

    reclaimed = service.recover_lease(operation_id, admin, reason="verifier run ended")
    assert reclaimed["ok"]
    recovered = service.execute_admin(
        "recover",
        {"submission_id": operation_id, "outcome": "inspect", "reason": "live evidence check"},
        principal=admin,
    )
    assert recovered["ok"]
    assert recovered["data"]["live_identity"]
    assert recovered["data"]["content_recovery_state"] == "confirmed_signoff"


def test_health_payload_contains_no_paths_or_credentials(tmp_path):
    service, _backend = _service(tmp_path)
    rendered = str(service.health())
    assert str(service.config.db_path) not in rendered
    assert str(service.config.honest_root) not in rendered
    assert "agent-token" not in rendered
    assert "admin-token" not in rendered


def test_server_close_drains_inflight_request_before_return(monkeypatch, tmp_path):
    service, _backend = _service(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocking_execute(command, arguments, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return result_envelope(command=command, data={"arguments": dict(arguments)})

    monkeypatch.setattr(service, "execute_agent", blocking_execute)
    server = build_server(service)
    listener = threading.Thread(target=server.serve_forever, daemon=False)
    listener.start()
    host, port = server.server_address
    result = {}

    def request():
        client = DishServiceClient(
            f"http://{host}:{port}", token="agent-secret", run_id="11111111-1111-4111-8111-111111111111"
        )
        result.update(client.execute("sections", {"agent": "gpt"}))

    requester = threading.Thread(target=request, daemon=False)
    closer = None
    requester.start()
    try:
        assert entered.wait(timeout=2)

        server.shutdown()
        closer = threading.Thread(target=server.server_close, daemon=False)
        closer.start()
        time.sleep(0.05)
        assert closer.is_alive(), "server_close returned before the active request drained"
    finally:
        release.set()
        if closer is None:
            server.shutdown()
            server.server_close()
        else:
            closer.join(timeout=2)
        requester.join(timeout=2)
        listener.join(timeout=2)

    assert closer is not None and not closer.is_alive()
    assert not requester.is_alive()
    assert not listener.is_alive()
    assert result["ok"] is True


def test_backup_restore_and_admin_argument_audit_are_available_over_private_http(tmp_path):
    import threading

    from dish_service.client import DishAdminServiceClient
    from dish_service.http import build_server

    service, _backend = _service(tmp_path)
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    client = DishAdminServiceClient(
        f"http://{host}:{port}", token="admin-secret", run_id="22222222-2222-4222-8222-222222222222"
    )
    try:
        created = client.create_backup(label="http")
        restored = client.restore_backup(created["data"]["backup"]["backup_id"])
        audited = client.record_argument_failure(
            "recover",
            DishRuleError("INVALID_ARGUMENT", "bad recovery request", rule="invalid_arguments"),
            submission_id=None,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert created["ok"] and restored["ok"] and audited["ok"] is False
    conn = initialize_database(service.config.db_path)
    try:
        row = conn.execute(
            "SELECT event_type, details FROM audit_events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["event_type"] == "dish-admin.recover"
    import json
    assert json.loads(row["details"])["actor_role"] == "marco"


def test_failed_restore_reports_proven_rollback_without_claiming_success(monkeypatch, tmp_path):
    import dish_service.backup as backup_module

    service, _backend = _service(tmp_path)
    created = service.create_backup(label="before-operation")
    assert created["ok"]
    backup_id = created["data"]["backup"]["backup_id"]

    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"},
    )
    assert started["ok"]

    original_initialize = backup_module.initialize_database
    calls = {"count": 0}

    def fail_post_replace_validation(path):
        calls["count"] += 1
        if calls["count"] == 3:
            raise RuntimeError("simulated post-replace validation failure")
        return original_initialize(path)

    monkeypatch.setattr(backup_module, "initialize_database", fail_post_replace_validation)
    result = service.restore_backup(backup_id)

    assert not result["ok"]
    assert result["errors"][0]["rule"] == "backup_restore_failed_rolled_back"
    assert result["errors"][0]["database_retained"] is True
    maintenance = service.health()["maintenance"]
    assert maintenance["ok"] is True
    assert maintenance["restore_recovery_required"] is False
    assert maintenance["restore_fault"] is None

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM operations WHERE operation_id=?", (started["submission_id"],)
        ).fetchone() is not None
    finally:
        conn.close()


def test_unproven_restore_rollback_disables_mutations(monkeypatch, tmp_path):
    import dish_service.backup as backup_module

    service, backend = _service(tmp_path)
    created = service.create_backup(label="restore-source")
    assert created["ok"]
    backup_id = created["data"]["backup"]["backup_id"]

    original_initialize = backup_module.initialize_database
    original_replace = backup_module.os.replace
    calls = {"count": 0}

    def fail_post_replace_validation(path):
        calls["count"] += 1
        if calls["count"] == 3:
            raise RuntimeError("simulated post-replace validation failure")
        return original_initialize(path)

    def fail_rollback_replace(source, destination):
        if ".rollback." in str(source):
            raise OSError("simulated rollback replacement failure")
        return original_replace(source, destination)

    monkeypatch.setattr(backup_module, "initialize_database", fail_post_replace_validation)
    monkeypatch.setattr(backup_module.os, "replace", fail_rollback_replace)

    result = service.restore_backup(backup_id)
    assert not result["ok"]
    assert result["errors"][0]["rule"] == "backup_restore_and_rollback_failed"
    assert result["errors"][0]["database_retained"] is False
    maintenance = service.health()["maintenance"]
    assert maintenance["ok"] is False
    assert maintenance["restore_recovery_required"] is True
    assert maintenance["restore_fault"]["rule"] == "backup_restore_and_rollback_failed"
    assert maintenance["restore_fault"]["details"]["database_retained"] is False

    writes_before = backend.writes
    moves_before = backend.moves
    blocked = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"},
    )
    assert not blocked["ok"]
    assert blocked["errors"][0]["rule"] == "service_restore_recovery_required"
    assert backend.writes == writes_before
    assert backend.moves == moves_before


def test_post_success_lease_failure_never_reverses_submit(monkeypatch, tmp_path):
    from dish_service.leases import LeaseManager

    service, _backend = _service(tmp_path)
    operation_id, verifier = _approved(service)

    def fail_terminal_release(self, operation_id, principal, *, reason="operation_terminal"):
        raise RuntimeError("simulated lease finalization failure")

    monkeypatch.setattr(LeaseManager, "release_terminal", fail_terminal_release)
    result = service.execute_agent(
        "submit", {"submission_id": operation_id}, principal=verifier
    )

    assert result["ok"]
    assert result["retryable"] is False
    assert result["allowed_actions"] == []
    assert "service_recovery_required" not in result["data"]
    assert result["data"]["service_cleanup_warning"] == {
        "kind": "lease_finalization",
        "operation_id": operation_id,
        "command": "submit",
        "error_type": "RuntimeError",
        "fallback_release_applied": True,
    }

    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT status,phase,completed_at FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        lease = conn.execute(
            "SELECT released_at FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(operation[:2]) == ("completed", "terminal")
    assert operation["completed_at"] is not None
    assert lease is None
