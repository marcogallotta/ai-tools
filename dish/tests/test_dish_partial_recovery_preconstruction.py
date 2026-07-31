from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

from dish_service.application import DishService
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request
from dish_tool.admin import DishAdminApplication
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.execution_provenance import operation_execution_provenance
from dish_tool.operation_execution import claim_operation_execution
from dish_tool.step6 import prepare_live
from tests._partial_recovery_helpers import (
    Backend,
    TASK,
    app,
    release_loader as _release_loader,
    service as _service,
    write,
    started_application as _started_application,
    fault_at_step as _fault_at_step,
)


def test_preconstruction_hold_audit_failure_rolls_back_and_exact_replay_applies_once(
    tmp_path, monkeypatch
):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal(
        owner_id="action", run_id="efefefef-efef-4fef-8fef-efefefefefef"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="72000000-0000-4000-8000-000000000001",
    )
    operation_id = started["submission_id"]
    hold_request = "72000000-0000-4000-8000-000000000002"
    arguments = {
        "agent": "gpt",
        "submission_id": operation_id,
        "route": "evidence",
        "reason": "Need authoritative evidence before construction",
        "resume_status": "pending-research",
    }

    import dish_tool.application_service as application_service
    import dish_tool.step8 as step8
    from dish_tool.invocation_audit import record_invocation_audit

    real_record_audit = step8.record_audit
    real_recovery_state = application_service.execution_recovery_state
    failed_once = False
    inspect_interleaved = False

    def fail_governed_hold_audit(*args, **kwargs):
        nonlocal failed_once
        if (
            not failed_once
            and kwargs.get("event_type") == "research.preconstruction_blocked"
        ):
            failed_once = True
            raise RuntimeError("injected governed hold audit failure")
        return real_record_audit(*args, **kwargs)

    def recover_after_interleaved_inspect(conn, *args, **kwargs):
        nonlocal inspect_interleaved
        if not inspect_interleaved and kwargs.get("failure_rule") == "RuntimeError":
            inspect_interleaved = True
            concurrent = initialize_database(service.config.db_path)
            try:
                record_invocation_audit(
                    concurrent,
                    surface="dish",
                    command="inspect",
                    result={
                        "ok": True,
                        "code": "OK",
                        "state": "open",
                        "retryable": False,
                        "errors": [],
                        "data": {"operation_id": operation_id},
                    },
                    task_gid="t",
                    submission_id=operation_id,
                    actor="gpt",
                    actor_run_id="abababab-abab-4bab-8bab-abababababab",
                )
            finally:
                concurrent.close()
        return real_recovery_state(conn, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(step8, "record_audit", fail_governed_hold_audit)
        fault.setattr(
            application_service,
            "execution_recovery_state",
            recover_after_interleaved_inspect,
        )
        failed = service.execute_agent(
            "reject",
            arguments,
            principal=principal,
            request_id=hold_request,
        )

    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["errors"][0]["rule"] == "operation_exact_replay_required"
    assert failed["retryable"] is True
    assert failed["data"]["request_replay_required"] is True
    assert failed["data"]["required_next_action"] == "retry_exact_request"
    assert failed["errors"][0]["recovery_required"] is False
    assert failed["data"]["effects_observed"] is False
    assert failed["data"].get("required_admin_action") is None
    assert failed["data"].get("required_admin_outcome") is None
    assert failed["data"].get("admin_recovery_lease_scope") is None
    assert failed["data"]["admin_recovery_immediately_executable"] is False
    assert inspect_interleaved is True

    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT phase,status FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert operation["phase"] == "prepare_required"
        assert operation["status"] == "open"
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_steps "
            "WHERE operation_id=? AND step_name='research_preconstruction_hold'",
            (operation_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE operation_id=? AND event_type='research.preconstruction_blocked'",
            (operation_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE operation_id=? AND event_type='dish.inspect'",
            (operation_id,),
        ).fetchone()[0] == 1
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (hold_request,)
        ).fetchone()
        assert request["status"] == "uncertain"
        execution = conn.execute(
            "SELECT * FROM operation_executions WHERE request_id=?", (hold_request,)
        ).fetchone()
        assert execution["status"] == "uncertain"
        assert execution["resolved_at"] is None
    finally:
        conn.close()

    applied = service.execute_agent(
        "reject",
        arguments,
        principal=principal,
        request_id=hold_request,
    )
    assert applied["ok"]
    assert applied["data"]["route"] == "evidence"
    assert applied["data"]["phase"] == "held_evidence"

    replayed = service.execute_agent(
        "reject",
        arguments,
        principal=principal,
        request_id=hold_request,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True

    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT phase,status FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert operation["phase"] == "held_evidence"
        assert operation["status"] == "open"
        hold_step = conn.execute(
            "SELECT * FROM operation_steps "
            "WHERE operation_id=? AND step_name='research_preconstruction_hold'",
            (operation_id,),
        ).fetchone()
        assert hold_step is not None
        assert hold_step["completed_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE operation_id=? AND event_type='research.preconstruction_blocked' "
            "AND governed_kind='decision' AND result_ok=1",
            (operation_id,),
        ).fetchone()[0] == 1
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (hold_request,)
        ).fetchone()
        assert request["status"] == "completed"
        assert request["resolution_result_json"] is not None
        resolved = json.loads(request["resolution_result_json"])
        assert resolved["ok"] is True
        assert resolved["data"]["phase"] == "held_evidence"
        execution = conn.execute(
            "SELECT * FROM operation_executions WHERE request_id=?", (hold_request,)
        ).fetchone()
        assert execution["status"] == "completed"
        assert execution["resolution_evidence_json"] is not None
    finally:
        conn.close()


def test_admin_pending_request_reconstructs_preconstruction_resolution_after_restart(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    import dish_tool.operation_execution as operation_execution
    import dish_tool.step8 as step8

    agent = ServicePrincipal(
        owner_id="action", run_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=agent,
        request_id="50000000-0000-4000-8000-000000000500",
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    held = service.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": operation_id,
            "route": "evidence",
            "reason": "need authoritative input",
            "resume_status": "pending-research",
        },
        principal=agent,
        request_id="50000000-0000-4000-8000-000000000501",
    )
    assert held["ok"]

    principal = ServicePrincipal(
        owner_id="marco-admin", run_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    )
    request_id = "50000000-0000-4000-8000-000000000502"
    arguments = {
        "submission_id": operation_id,
        "detail": "Marco supplied the missing authority",
        "resume_status": "pending-research",
    }
    real_complete = step8.complete_operation_step

    def terminate_after_transition(conn, current_operation, step_name):
        if step_name == "research_preconstruction_hold_resolution":
            raise SystemExit("terminated before resolution completion")
        return real_complete(conn, current_operation, step_name)

    with monkeypatch.context() as killed:
        killed.setattr(step8, "complete_operation_step", terminate_after_transition)
        with pytest.raises(SystemExit):
            service.execute_admin(
                "supply-evidence",
                arguments,
                principal=principal,
                request_id=request_id,
            )

    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT status,phase FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(operation) == ("open", "prepare_required")
        pending = conn.execute(
            "SELECT completed_at FROM operation_steps "
            "WHERE operation_id=? AND step_name='research_preconstruction_hold_resolution'",
            (operation_id,),
        ).fetchone()
        assert pending["completed_at"] is None
    finally:
        conn.close()

    restarted = DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=_release_loader(service.config.honest_root),
    )
    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        recovered = restarted.execute_admin(
            "supply-evidence",
            arguments,
            principal=principal,
            request_id=request_id,
        )
    assert recovered["code"] == "BACKEND_UNCERTAIN"
    assert recovered["data"]["command"] == "supply-evidence"
    assert recovered["data"]["local_state_committed"] is True
    assert recovered["data"]["write_committed"] is False
    assert recovered["data"]["move_committed"] is False
    assert recovered["data"]["failed_step"] == (
        "research_preconstruction_hold_resolution"
    )
    assert recovered["data"]["required_admin_action"] == "recover"
    assert recovered["data"]["required_admin_outcome"] == "applied"
    assert recovered["data"]["safe_to_retry"] is False

    exact = restarted.execute_admin(
        "supply-evidence",
        arguments,
        principal=principal,
        request_id=request_id,
    )
    assert exact["data"]["request_replayed"] is True
    assert exact["data"]["execution_id"] == recovered["data"]["execution_id"]

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        reconciled = restarted.execute_admin(
            "recover",
            {
                "submission_id": operation_id,
                "outcome": "applied",
                "reason": "reconcile interrupted hold resolution",
            },
            principal=principal,
            request_id="50000000-0000-4000-8000-000000000503",
        )
    assert reconciled["ok"]
    assert any(
        action.get("step") == "research_preconstruction_hold_resolution"
        for action in reconciled["data"]["actions"]
    )

