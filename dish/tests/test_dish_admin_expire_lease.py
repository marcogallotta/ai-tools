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
def test_expire_exact_lease_releases_row_and_preserves_workflow(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]

    result = service.expire_lease(
        _admin(),
        lease_id=lease_id,
        reason="agent process died",
        request_id=EXPIRY_REQUEST,
    )

    assert result["ok"] is True
    assert result["allowed_actions"] == []
    assert result["submission_id"] == started["submission_id"]
    assert result["state"] == "open"
    assert result["data"]["outcome"] == "released"
    assert result["data"]["lease"]["release_reason"] == "admin expiry: agent process died"
    assert result["data"]["ownership_transferred"] is False

    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        operation = conn.execute(
            "SELECT status,phase FROM operations WHERE operation_id=?",
            (started["submission_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert lease["released_at"] is not None
    assert lease["release_reason"] == "admin expiry: agent process died"
    assert tuple(operation) == ("open", "prepare_required")
@pytest.mark.smoke
def test_previous_eligible_run_can_reacquire_after_release(tmp_path):
    service, backend = _service(tmp_path)
    owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    assert service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )["ok"]

    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=owner,
        request_id=OTHER_REQUEST,
    )

    assert prepared["ok"] is True
    assert backend.writes == 1
@pytest.mark.smoke
def test_exact_replay_never_touches_replacement_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner, started = _start(service)
    operation_id = started["submission_id"]
    old_lease_id = started["data"]["service_lease"]["lease_id"]
    first = service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert first["data"]["released"] is True

    conn = initialize_database(service.config.db_path)
    try:
        replacement = LeaseManager(conn).acquire(operation_id, owner)
    finally:
        conn.close()

    replay = service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert replay["data"]["request_replayed"] is True
    assert replay["data"]["lease"]["lease_id"] == old_lease_id

    conn = initialize_database(service.config.db_path)
    try:
        active = conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert active["lease_id"] == replacement["lease_id"]
@pytest.mark.smoke
def test_task_noop_replay_never_touches_later_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner, started = _start(service)
    old_lease_id = started["data"]["service_lease"]["lease_id"]
    service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="cleanup", request_id=EXPIRY_REQUEST
    )
    no_op_request = str(uuid.uuid4())
    no_op = service.expire_lease(
        _admin(), task_gid=TASK_GID, reason="nothing active", request_id=no_op_request
    )
    assert no_op["data"]["outcome"] == "no_active_lease"

    conn = initialize_database(service.config.db_path)
    try:
        replacement = LeaseManager(conn).acquire(started["submission_id"], owner)
    finally:
        conn.close()

    replay = service.expire_lease(
        _admin(), task_gid=TASK_GID, reason="nothing active", request_id=no_op_request
    )
    assert replay["data"]["request_replayed"] is True
    conn = initialize_database(service.config.db_path)
    try:
        active = conn.execute(
            "SELECT lease_id FROM service_leases WHERE task_gid=? AND released_at IS NULL",
            (TASK_GID,),
        ).fetchone()
    finally:
        conn.close()
    assert active["lease_id"] == replacement["lease_id"]
@pytest.mark.smoke
def test_same_request_id_requires_same_admin_run(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    first = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert first["ok"]

    same = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    different = service.expire_lease(
        _admin(OTHER_ADMIN_RUN),
        lease_id=lease_id,
        reason="owner dead",
        request_id=EXPIRY_REQUEST,
    )
    assert same["data"]["request_replayed"] is True
    assert different["code"] == "CONFLICT"
    assert different["errors"][0]["rule"] == "service_request_identity_conflict"
@pytest.mark.smoke
@pytest.mark.parametrize("claim_live", [True, False])
def test_execution_claim_guard_uses_existing_liveness_and_preserves_claim(
    tmp_path, monkeypatch, claim_live
):
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
            (operation_id, str(uuid.uuid4()), "prepare", "host", 123, "start", "now"),
        )
    finally:
        conn.close()
    monkeypatch.setattr(
        "dish_tool.operation_execution.process_identity_is_live",
        lambda _identity: claim_live,
    )

    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )

    conn = initialize_database(service.config.db_path)
    try:
        active = conn.execute(
            "SELECT released_at FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM operation_execution_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert claim_count == 1
    if claim_live:
        assert result["code"] == "CONFLICT"
        assert result["retryable"] is True
        assert result["errors"][0]["rule"] == "operation_mutation_in_progress"
        assert active["released_at"] is None
    else:
        assert result["ok"] is True
        assert active["released_at"] is not None
@pytest.mark.smoke
def test_result_persistence_failure_rolls_back_release(tmp_path, monkeypatch):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]

    def fail_completion(*_args, **_kwargs):
        raise RuntimeError("completion failed")

    monkeypatch.setattr("dish_service.application.complete_request", fail_completion)
    with pytest.raises(RuntimeError, match="completion failed"):
        service.expire_lease(
            _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
        )

    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT released_at FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        request = conn.execute(
            "SELECT status FROM service_requests WHERE request_id=?", (EXPIRY_REQUEST,)
        ).fetchone()
    finally:
        conn.close()
    assert lease["released_at"] is None
    assert request["status"] == "pending"
@pytest.mark.smoke
def test_expiry_service_path_never_constructs_backend_or_release(tmp_path):
    seed, _backend = _service(tmp_path)
    _owner, started = _start(seed)
    lease_id = started["data"]["service_lease"]["lease_id"]

    def bomb(*_args, **_kwargs):
        raise AssertionError("workflow dependency constructed")

    service, _unused = _service(
        tmp_path, backend_factory=bomb, release_loader=bomb
    )
    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert result["ok"] is True
@pytest.mark.smoke
def test_service_canonicalizes_reason_before_request_hash(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]

    first = service.expire_lease(
        _admin(), lease_id=lease_id, reason="  owner dead  ", request_id=EXPIRY_REQUEST
    )
    replay = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )

    assert first["data"]["lease"]["release_reason"] == "admin expiry: owner dead"
    assert replay["data"]["request_replayed"] is True
