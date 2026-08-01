from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest


from dish_service.application import DishService
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request
from dish_tool.admin import DishAdminApplication
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.execution_provenance import operation_execution_provenance
from dish_tool.operation_execution import claim_operation_execution
from dish_tool.step6 import prepare_live
from tests.support.partial_recovery import (
    Backend,
    TASK,
    app,
    release_loader as _release_loader,
    service as _service,
    write,
    started_application as _started_application,
    fault_at_step as _fault_at_step,
)


@pytest.mark.smoke
@pytest.mark.invariant_partial_effect_recovery
@pytest.mark.parametrize(
    ("fault_step", "write_committed", "cycle_created", "move_committed"),
    [
        ("candidate_write", True, False, False),
        ("handoff_validation", True, False, False),
        ("verification_cycle", True, True, False),
        ("verification_handoff", True, True, True),
    ],
)
def test_partial_failures_report_request_scoped_durable_evidence(
    tmp_path,
    monkeypatch,
    fault_step,
    write_committed,
    cycle_created,
    move_committed,
):
    application, backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "candidate.md", TASK)

    with monkeypatch.context() as fault:
        _fault_at_step(fault, fault_step)
        result = application.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=operation_id,
            file_path=candidate,
        )

    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["retryable"] is False
    assert result["data"]["write_committed"] is write_committed
    assert result["data"]["cycle_created"] is cycle_created
    assert result["data"]["move_committed"] is move_committed
    assert result["data"]["failed_step"] == fault_step
    assert result["data"]["authoritative_task_identity"]
    assert result["data"]["authoritative_identity_source"] == (
        "execution_confirmed_content_version"
    )
    assert result["data"]["required_admin_action"] == "recover"
    assert result["data"]["safe_to_retry"] is False
    assert result["allowed_actions"] == []

    execution = application.conn.execute(
        "SELECT * FROM operation_executions WHERE execution_id=?",
        (result["data"]["execution_id"],),
    ).fetchone()
    assert execution["status"] == "uncertain"
    assert execution["request_id"] is None
    assert application.conn.execute(
        "SELECT COUNT(*) FROM operation_execution_claims"
    ).fetchone()[0] == 0

    writes_before = backend.writes
    moves_before = backend.moves
    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    recovered = admin.execute(
        "recover",
        submission_id=operation_id,
        outcome="applied",
        reason="fault-window reconciliation",
    )
    assert recovered["ok"]
    assert backend.writes == writes_before
    if fault_step == "verification_handoff":
        assert backend.moves == moves_before
    else:
        assert backend.moves <= moves_before + 1
    assert application.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 1


