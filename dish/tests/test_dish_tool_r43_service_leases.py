from datetime import datetime, timedelta, timezone
import threading

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend, TASK


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _service(tmp_path, backend, *, clock=None, ttl=60):
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    return DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            lease_ttl_seconds=ttl,
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
        lease_now=None if clock is None else clock.now,
    )


def _principal(name, run):
    return ServicePrincipal(owner_id=name, run_id=run)


def test_two_clients_cannot_start_and_lease_same_task(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    barrier = threading.Barrier(2)
    results = []

    def run(principal):
        barrier.wait()
        results.append(service.execute_agent(
            "start",
            {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": principal.run_id},
            principal=principal,
        ))

    threads = [
        threading.Thread(target=run, args=(_principal("client-a", "run-a"),)),
        threading.Thread(target=run, args=(_principal("client-b", "run-b"),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sum(result["ok"] for result in results) == 1
    loser = next(result for result in results if not result["ok"])
    assert loser["code"] == "CONFLICT"

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations WHERE status='open'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM service_leases WHERE released_at IS NULL").fetchone()[0] == 1
    finally:
        conn.close()


def test_lease_owner_blocks_another_client_before_prepare_mutation(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    owner = _principal("constructor", "run-1")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run-1"},
        principal=owner,
    )
    assert started["ok"]
    result = service.execute_agent(
        "prepare",
        {
            "agent": "gpt", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "file_text": TASK,
        },
        principal=_principal("other-client", "run-2"),
    )
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "service_lease_owner_mismatch"
    assert backend.writes == 0
    assert backend.moves == 0


def test_workflow_handoff_releases_owner_lease_but_keeps_task_operation_lock(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    constructor = _principal("constructor", "constructor-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "constructor-run"},
        principal=constructor,
    )
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "file_text": TASK,
        },
        principal=constructor,
    )
    assert prepared["ok"]
    assert prepared["data"]["service_lease"] is None

    conn = initialize_database(service.config.db_path)
    try:
        op = conn.execute("SELECT status,phase FROM operations WHERE operation_id=?", (started["submission_id"],)).fetchone()
        assert tuple(op) == ("open", "await_verification")
        assert conn.execute("SELECT COUNT(*) FROM service_leases WHERE released_at IS NULL").fetchone()[0] == 0
    finally:
        conn.close()

    verifier = _principal("verifier", "verify-run")
    review = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification", "run_id": "verify-run"},
        principal=verifier,
    )
    assert review["ok"]
    assert review["data"]["service_lease"]["owner_id"] == "verifier"


def test_lease_renewal_expiry_and_admin_recovery_are_deterministic(tmp_path):
    clock = Clock()
    backend = Backend()
    service = _service(tmp_path, backend, clock=clock, ttl=30)
    owner = _principal("owner", "run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"},
        principal=owner,
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    first_expiry = started["data"]["service_lease"]["expires_at"]

    clock.advance(10)
    renewed = service.renew_lease(operation_id, owner)
    assert renewed["ok"]
    assert renewed["data"]["service_lease"]["expires_at"] > first_expiry
    assert renewed["task_gid"] == started["task_gid"]

    not_stale = service.recover_lease(
        operation_id, _principal("admin", "recovery-1"), reason="premature"
    )
    assert not_stale["code"] == "CONFLICT"
    assert not_stale["task_gid"] == "t"
    assert not_stale["submission_id"] == operation_id
    assert not_stale["errors"][0]["rule"] == "service_lease_not_stale"

    clock.advance(31)
    expired = service.renew_lease(operation_id, owner)
    assert expired["code"] == "CONFLICT"
    assert expired["task_gid"] == "t"
    assert expired["submission_id"] == operation_id
    assert expired["errors"][0]["rule"] == "service_lease_expired"

    recovered = service.recover_lease(
        operation_id, _principal("admin", "recovery-2"), reason="owner confirmed dead"
    )
    assert recovered["ok"]
    assert recovered["task_gid"] == "t"
    assert recovered["state"] == "open"
    assert recovered["data"]["service_lease"] is None
    assert recovered["data"]["ownership_transferred"] is False


def test_terminal_operation_renewal_reports_terminal_wrong_state(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    owner = _principal("owner", "run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"},
        principal=owner,
    )
    operation_id = started["submission_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            "UPDATE operations SET status='completed', phase='terminal', completed_at='now', "
            "terminal_outcome='test' WHERE operation_id=?",
            (operation_id,),
        )
        conn.execute(
            "UPDATE service_leases SET released_at='now', release_reason='test' "
            "WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        )
    finally:
        conn.close()
    result = service.renew_lease(operation_id, owner)
    assert result["code"] == "WRONG_STATE"
    assert result["state"] == "completed"
    assert result["errors"] == [{"rule": "operation_not_open", "actual": "completed"}]


def test_task_lock_cannot_release_before_terminal_completion(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    owner = _principal("owner", "run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"},
        principal=owner,
    )
    conn = initialize_database(service.config.db_path)
    try:
        leases = LeaseManager(conn, ttl_seconds=60)
        with pytest.raises(Exception) as exc:
            leases.release_terminal(started["submission_id"], owner)
        assert getattr(exc.value, "rule", None) == "service_task_lock_active"
        assert leases.active_for_operation(started["submission_id"]) is not None
    finally:
        conn.close()


def test_service_restart_preserves_active_lease(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    owner = _principal("owner", "run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"},
        principal=owner,
    )
    restarted = DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )
    renewed = restarted.renew_lease(started["submission_id"], owner)
    assert renewed["ok"]
    assert renewed["data"]["service_lease"]["operation_id"] == started["submission_id"]
    assert renewed["task_gid"] == started["task_gid"]
