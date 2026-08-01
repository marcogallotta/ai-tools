from __future__ import annotations

import socket
import threading
import uuid

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.database import initialize_database
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK
from tests.support.lease_expiry import (
    ADMIN_RUN,
    EXPIRY_REQUEST,
    OWNER_RUN,
    START_REQUEST,
    TASK_GID,
    _admin,
    _service,
    _start,

)

OTHER_ADMIN_RUN = "33333333-3333-4333-8333-333333333333"
OTHER_REQUEST = "66666666-6666-4666-8666-666666666666"

@pytest.mark.smoke
def test_fresh_task_request_can_release_replacement_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner, started = _start(service)
    operation_id = started["submission_id"]
    old_lease_id = started["data"]["service_lease"]["lease_id"]
    assert service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="old owner dead", request_id=EXPIRY_REQUEST
    )["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        replacement = LeaseManager(conn).acquire(operation_id, owner)
    finally:
        conn.close()

    result = service.expire_lease(
        _admin(), task_gid=TASK_GID, reason="release replacement", request_id=OTHER_REQUEST
    )
    assert result["data"]["outcome"] == "released"
    assert result["data"]["lease"]["lease_id"] == replacement["lease_id"]
@pytest.mark.smoke
def test_already_released_exact_target_preserves_original_release(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    first = service.expire_lease(
        _admin(), lease_id=lease_id, reason="first reason", request_id=EXPIRY_REQUEST
    )

    second = service.expire_lease(
        _admin(), lease_id=lease_id, reason="second reason", request_id=OTHER_REQUEST
    )

    assert second["ok"] is True
    assert second["data"]["outcome"] == "already_released"
    assert second["data"]["released"] is False
    assert second["data"]["lease"]["released_at"] == first["data"]["lease"]["released_at"]
    assert second["data"]["lease"]["release_reason"] == "admin expiry: first reason"
@pytest.mark.smoke
def test_unknown_exact_lease_returns_canonical_not_found(tmp_path):
    service, _backend = _service(tmp_path)
    unknown = str(uuid.uuid4())
    result = service.expire_lease(
        _admin(), lease_id=unknown, reason="operator lookup", request_id=EXPIRY_REQUEST
    )

    assert result["ok"] is False
    assert result["code"] == "NOT_FOUND"
    assert result["task_gid"] is None
    assert result["submission_id"] is None
    assert result["state"] is None
    assert result["allowed_actions"] == []
    assert result["data"]["request_id"] == EXPIRY_REQUEST
    assert result["errors"] == [{"rule": "service_lease_not_found", "lease_id": unknown}]
@pytest.mark.smoke
def test_active_lease_on_terminal_operation_can_be_released(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            "UPDATE operations SET status='completed', phase='terminal', completed_at='now', "
            "terminal_outcome='test' WHERE operation_id=?",
            (started["submission_id"],),
        )
    finally:
        conn.close()

    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="terminal cleanup", request_id=EXPIRY_REQUEST
    )

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["data"]["outcome"] == "released"
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("principal", "reason"),
    [
        (ServicePrincipal("other-admin", ADMIN_RUN), "owner dead"),
        (_admin(), "different reason"),
    ],
)
def test_request_identity_conflicts_on_principal_or_reason_change(
    tmp_path, principal, reason
):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    assert service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )["ok"]

    conflict = service.expire_lease(
        principal, lease_id=lease_id, reason=reason, request_id=EXPIRY_REQUEST
    )
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"
@pytest.mark.invariant_lease_authority
@pytest.mark.smoke
def test_duplicate_request_id_concurrency_returns_one_release_and_one_replay(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(
            service.expire_lease(
                _admin(),
                lease_id=lease_id,
                reason="owner dead",
                request_id=EXPIRY_REQUEST,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for item in threads:
        item.start()
    for item in threads:
        item.join(timeout=5)

    assert len(results) == 2
    assert all(result["ok"] for result in results)
    assert sum(bool(result["data"].get("request_replayed")) for result in results) == 1
    assert {result["data"]["lease"]["lease_id"] for result in results} == {lease_id}
@pytest.mark.smoke
def test_foreign_host_execution_claim_fails_closed(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    operation_id = started["submission_id"]
    lease_id = started["data"]["service_lease"]["lease_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,acquired_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                operation_id,
                str(uuid.uuid4()),
                "prepare",
                "definitely-another-host",
                123,
                "start",
                "now",
            ),
        )
    finally:
        conn.close()

    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "operation_mutation_in_progress"
@pytest.mark.smoke
def test_permission_denied_process_check_fails_closed(tmp_path, monkeypatch):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    operation_id = started["submission_id"]
    lease_id = started["data"]["service_lease"]["lease_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,acquired_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                operation_id,
                str(uuid.uuid4()),
                "prepare",
                socket.gethostname(),
                123,
                "fallback:123",
                "now",
            ),
        )
    finally:
        conn.close()

    monkeypatch.setattr("dish_tool.recovery._linux_process_start", lambda _pid: None)

    def denied(_pid, _signal):
        raise PermissionError("denied")

    monkeypatch.setattr("dish_tool.recovery.os.kill", denied)
    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "operation_mutation_in_progress"
