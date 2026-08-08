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
from dish_tool.database_initialization import initialize_database
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




def _prepare_uncertain_write_under_action_lease(service, backend, monkeypatch):
    action = ServicePrincipal(
        owner_id="action", run_id="abababab-abab-4bab-8bab-abababababab"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=action,
        request_id="71000000-0000-4000-8000-000000000001",
    )
    operation_id = started["submission_id"]
    import dish_tool.step6 as step6
    from dish_tool.errors import DishRuleError

    real_record_actor_fact = step6.record_actor_fact
    failed_once = False

    def fail_after_confirmed_write(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise DishRuleError(
                "CONFLICT",
                "fault after confirmed write",
                rule="injected_confirmed_write_failure",
            )
        return real_record_actor_fact(*args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(step6, "record_actor_fact", fail_after_confirmed_write)
        uncertain = service.execute_agent(
            "prepare",
            {
                "agent": "gpt",
                "model": "gpt-5.6-sol",
                "submission_id": operation_id,
                "file_text": backend.title + "\n" + backend.notes,
            },
            principal=action,
            request_id="71000000-0000-4000-8000-000000000002",
        )
    assert uncertain["code"] == "BACKEND_UNCERTAIN"
    assert uncertain["data"]["required_admin_action"] == "recover"
    assert uncertain["data"]["admin_recovery_lease_scope"] == "exact_uncertain_execution"
    assert uncertain["data"]["admin_recovery_immediately_executable"] is True
    assert backend.writes == 1
    return action, operation_id, uncertain


def _active_lease_id(service, operation_id, action):
    conn = initialize_database(service.config.db_path)
    try:
        actor_lease = conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
        assert actor_lease["owner_id"] == action.owner_id
        assert actor_lease["run_id"] == action.run_id
        return actor_lease["lease_id"]
    finally:
        conn.close()


def _recover_and_replay_action_write(service, operation_id, backend):
    admin = ServicePrincipal(
        owner_id="admin", run_id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    )
    request_id = "71000000-0000-4000-8000-000000000003"
    arguments = {
        "submission_id": operation_id,
        "outcome": "applied",
        "reason": "reconcile confirmed Action write",
    }
    recovered = service.execute_admin(
        "recover", arguments, principal=admin, request_id=request_id
    )
    replayed = service.execute_admin(
        "recover", arguments, principal=admin, request_id=request_id
    )
    assert recovered["ok"]
    assert replayed["ok"] and replayed["data"]["request_replayed"] is True
    assert backend.writes == 1


def _assert_action_lease_handoff(service, operation_id, action, lease_id, execution_id):
    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone() is None
        released_lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        assert released_lease["owner_id"] == action.owner_id
        assert released_lease["run_id"] == action.run_id
        assert released_lease["released_at"] is not None
        assert released_lease["release_reason"].startswith("exact_recovery_handoff:")
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='operation.recovery'",
            (operation_id,),
        ).fetchone()[0] == 1
        execution = conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?", (execution_id,)
        ).fetchone()
        assert execution["status"] == "completed"
        assert execution["resolution_evidence_json"] is not None
    finally:
        conn.close()


def _assert_fresh_verifier_claims(service):
    verifier = ServicePrincipal(
        owner_id="action", run_id="dededede-dede-4ded-8ded-dededededede"
    )
    verification = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent verifier run",
        },
        principal=verifier,
        request_id="71000000-0000-4000-8000-000000000004",
    )
    assert verification["ok"]
    assert verification["data"]["service_lease"]["run_id"] == verifier.run_id


def test_admin_recover_executes_immediately_under_exact_live_action_lease(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    action, operation_id, uncertain = _prepare_uncertain_write_under_action_lease(
        service, backend, monkeypatch
    )
    assert uncertain["data"]["execution_id"]
    lease_id = _active_lease_id(service, operation_id, action)
    _recover_and_replay_action_write(service, operation_id, backend)
    _assert_action_lease_handoff(
        service, operation_id, action, lease_id, uncertain["data"]["execution_id"]
    )
    _assert_fresh_verifier_claims(service)


def _prepare_verifier_for_evidence_handoff(service, backend):
    constructor = ServicePrincipal(
        owner_id="action", run_id="11111111-1111-4111-8111-111111111111"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id="73000000-0000-4000-8000-000000000001",
    )
    operation_id = started["submission_id"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": backend.title + "\n" + backend.notes,
        },
        principal=constructor,
        request_id="73000000-0000-4000-8000-000000000002",
    )
    assert prepared["ok"]
    verifier = ServicePrincipal(
        owner_id="action", run_id="22222222-2222-4222-8222-222222222222"
    )
    reviewed = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent verifier run",
        },
        principal=verifier,
        request_id="73000000-0000-4000-8000-000000000003",
    )
    assert reviewed["ok"]
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
        request_id="73000000-0000-4000-8000-000000000004",
    )
    assert inspected["ok"]
    return operation_id, verifier


def _fail_confirmed_evidence_hold(service, operation_id, verifier, monkeypatch):
    import dish_tool.step8 as step8
    from dish_tool.errors import DishRuleError

    real_complete = step8.complete_operation_step
    failed_once = False

    def fail_after_confirmed_hold_write(conn, current_operation, step_name):
        nonlocal failed_once
        if not failed_once and step_name.startswith("route_write:"):
            failed_once = True
            raise DishRuleError(
                "CONFLICT",
                "fault after confirmed Evidence hold write",
                rule="injected_evidence_handoff_failure",
            )
        return real_complete(conn, current_operation, step_name)

    with monkeypatch.context() as fault:
        fault.setattr(step8, "complete_operation_step", fail_after_confirmed_hold_write)
        uncertain = service.execute_agent(
            "reject",
            {
                "agent": "codex",
                "submission_id": operation_id,
                "route": "evidence",
                "reason": "need authoritative evidence",
                "resume_status": "pending-verification",
            },
            principal=verifier,
            request_id="73000000-0000-4000-8000-000000000005",
        )
    assert uncertain["code"] == "BACKEND_UNCERTAIN"
    assert uncertain["data"]["required_admin_action"] == "recover"
    assert uncertain["data"]["admin_recovery_immediately_executable"] is True


def _recover_and_supply_evidence(service, operation_id):
    admin = ServicePrincipal(
        owner_id="admin", run_id="33333333-3333-4333-8333-333333333333"
    )
    recovered = service.execute_admin(
        "recover",
        {
            "submission_id": operation_id,
            "outcome": "applied",
            "reason": "reconcile confirmed Evidence hold write",
        },
        principal=admin,
        request_id="73000000-0000-4000-8000-000000000006",
    )
    assert recovered["ok"] and recovered["state"] == "open"
    assert recovered["data"]["service_lease"] is None
    supplied = service.execute_admin(
        "supply-evidence",
        {
            "submission_id": operation_id,
            "detail": "Marco supplied the missing evidence",
            "resume_status": "pending-verification",
        },
        principal=admin,
        request_id="73000000-0000-4000-8000-000000000007",
    )
    assert supplied["ok"]
    assert supplied["data"]["service_lease"] is None


def test_exact_recovery_to_evidence_handoff_releases_verifier_lease_for_admin_continuation(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    operation_id, verifier = _prepare_verifier_for_evidence_handoff(service, backend)
    assert operation_id
    _fail_confirmed_evidence_hold(service, operation_id, verifier, monkeypatch)
    _recover_and_supply_evidence(service, operation_id)
