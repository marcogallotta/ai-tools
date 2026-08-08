from __future__ import annotations

import json

import pytest

from dish_service.application import DishService
from dish_service.backup import BackupManager
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from tests.support.service_scenarios import (
    OTHER_REQUEST_ID,
    REQUEST_ID,
    RUN_ID,
    complete_service_submission as _complete_service_submission,
    service as _service,
)
from tests.support.service_foundation import _release_loader
from tests.support.request_restore import Backend
from tests.support.planning import Backend as PlanningBackend, PLANNING, app, write
from tests.support.submission import _signed
from tests.support.planning import Backend as PlanningBackend, TASK, app, release
from tests.support.planning import Backend as PlanningBackend, TASK, app

@pytest.mark.parametrize(
    ("route", "admin_command", "held_phase"),
    [
        ("evidence", "supply-evidence", "held_evidence"),
        ("human-review", "record-human-decision", "held_human"),
    ],
)
def test_initial_research_can_hold_before_prepare_and_resume_same_operation(
    tmp_path, route, admin_command, held_phase
):
    from dish_tool.admin import DishAdminApplication

    lines = TASK.splitlines()
    backend = PlanningBackend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial"
    )
    operation_id = started["submission_id"]
    writes = backend.writes
    held = application.execute(
        "reject",
        agent="gpt",
        submission_id=operation_id,
        route=route,
        reason="Need authoritative input before constructing a candidate",
        resume_status="pending-research",
        **({
            "human_review_confirmed": True,
            "human_review_basis": "The remaining pre-construction choice requires Marco's authority.",
            "repairs_considered": "Within-authority research routes were considered and cannot settle that choice.",
        } if route == "human-review" else {}),
    )
    assert held["ok"]
    assert held["data"]["description"] == "Research blocked before construction"
    assert held["data"]["candidate_content_existed"] is False
    assert held["state"] == "open"
    assert backend.writes == writes
    assert application.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 0

    inspected = application.execute(
        "inspect", agent="gpt", submission_id=operation_id
    )
    view = inspected["data"]["authoritative_view"]
    assert view["phase"] == held_phase
    assert view["preconstruction_hold"] is True
    assert view["research_hold"]["candidate_content_existed"] is False
    assert view["research_hold"]["resume_status"] == "pending-research"

    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: release(tmp_path / "honest"),
    )
    resolved = admin.execute(
        admin_command,
        submission_id=operation_id,
        detail="Required input supplied",
        resume_status="pending-research",
    )
    assert resolved["ok"]
    assert resolved["data"]["candidate_content_existed"] is False
    assert resolved["data"]["phase"] == "prepare_required"
    assert backend.writes == writes

    resumed = application.execute(
        "inspect", agent="gpt", submission_id=operation_id
    )["data"]["authoritative_view"]
    assert resumed["phase"] == "prepare_required"
    assert "prepare" in resumed["legal_actions"]
def test_preconstruction_hold_rejects_wrong_resume_status_without_false_cycle(tmp_path):

    lines = TASK.splitlines()
    backend = PlanningBackend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial"
    )
    result = application.execute(
        "reject",
        agent="gpt",
        submission_id=started["submission_id"],
        route="evidence",
        reason="Need evidence",
        resume_status="pending-verification",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "preconstruction_resume_status_invalid"
    assert application.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?",
        (started["submission_id"],),
    ).fetchone()[0] == 0
def test_service_preconstruction_hold_persists_request_identity_and_timestamp(tmp_path):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal(owner_id="action", run_id=RUN_ID)
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="20000000-0000-4000-8000-000000000001",
    )
    assert started["ok"]
    hold_request_id = "20000000-0000-4000-8000-000000000002"
    held = service.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": started["submission_id"],
            "route": "evidence",
            "reason": "Need authoritative source before construction",
            "resume_status": "pending-research",
        },
        principal=principal,
        request_id=hold_request_id,
    )
    assert held["ok"]
    assert held["data"]["request_id"] == hold_request_id
    assert held["data"]["timestamp"].endswith("Z")

    inspected = service.execute_agent(
        "inspect",
        {"agent": "gpt", "submission_id": started["submission_id"]},
        principal=principal,
    )
    record = inspected["data"]["authoritative_view"]["research_hold"]
    assert record["request_id"] == hold_request_id
    assert record["timestamp"] == held["data"]["timestamp"]
