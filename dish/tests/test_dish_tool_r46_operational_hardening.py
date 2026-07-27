from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
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


def _service(tmp_path, backend=None, *, clock=None, ttl=60):
    backend = backend or Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "managed-backups",
            lease_ttl_seconds=ttl,
            agent_token="agent",
            admin_token="admin",
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
        {"agent": "codex", "task_gid": "t", "kind": "verification", "run_id": "verifier-run"},
        principal=verifier,
    )
    assert review["ok"]
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
    assert startup["database"]["schema_version"] == 20
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
