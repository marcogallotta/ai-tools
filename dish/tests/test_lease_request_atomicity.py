from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

import dish_service.application as application_module
from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.database import confirm_task_content, create_operation
from dish_tool.database_schema import initialize_database
from dish_tool.models import OperationActors


RUN_ID = "11111111-1111-4111-8111-111111111111"


def _service_with_lease(tmp_path, *, acquired_at: datetime):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    identity = confirm_task_content(
        conn,
        task_gid="task-lease",
        title="Dish",
        notes="Notes",
        schema_version="2",
        boundary="test",
    )
    operation = create_operation(
        conn,
        task_gid="task-lease",
        operation_kind="initial",
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="section",
        actors=OperationActors(researcher_agent="gpt", run_id=RUN_ID),
    )
    principal = ServicePrincipal(owner_id="owner", run_id=RUN_ID)
    lease = LeaseManager(conn, now=lambda: acquired_at).acquire(
        operation["operation_id"], principal
    )
    conn.close()
    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=tmp_path),
        backend_factory=lambda: object(),
        lease_now=lambda: acquired_at + timedelta(minutes=1),
    )
    return service, principal, operation["operation_id"], lease


def _fail_completion(*_args, **_kwargs):
    raise RuntimeError("request result persistence interrupted")


def test_renew_rolls_back_when_request_result_cannot_commit(monkeypatch, tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    service, principal, operation_id, original = _service_with_lease(
        tmp_path, acquired_at=now
    )
    request_id = str(uuid.uuid4())
    monkeypatch.setattr(application_module, "complete_request", _fail_completion)

    with pytest.raises(RuntimeError, match="persistence interrupted"):
        service.renew_lease(operation_id, principal, request_id=request_id)

    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT renewed_at,expires_at FROM service_leases WHERE lease_id=?",
            (original["lease_id"],),
        ).fetchone()
        request = conn.execute(
            "SELECT status,result_json FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
    finally:
        conn.close()
    assert lease["renewed_at"] == original["renewed_at"]
    assert lease["expires_at"] == original["expires_at"]
    assert request["status"] == "pending"
    assert request["result_json"] is None


def test_recover_rolls_back_when_request_result_cannot_commit(monkeypatch, tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    service, principal, operation_id, original = _service_with_lease(
        tmp_path, acquired_at=now
    )
    service.lease_now = lambda: now + timedelta(hours=1)
    request_id = str(uuid.uuid4())
    monkeypatch.setattr(application_module, "complete_request", _fail_completion)

    with pytest.raises(RuntimeError, match="persistence interrupted"):
        service.recover_lease(
            operation_id,
            principal,
            reason="stale owner",
            request_id=request_id,
        )

    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT released_at,release_reason FROM service_leases WHERE lease_id=?",
            (original["lease_id"],),
        ).fetchone()
        request = conn.execute(
            "SELECT status,result_json FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
    finally:
        conn.close()
    assert lease["released_at"] is None
    assert lease["release_reason"] is None
    assert request["status"] == "pending"
    assert request["result_json"] is None
