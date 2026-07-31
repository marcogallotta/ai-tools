from __future__ import annotations

import socket
import uuid

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.admin_cli import build_parser
from dish_tool.application_service import CurrentWorkflowService
from dish_tool.database import declare_operation_step
from dish_tool.database_schema import initialize_database
from tests._workflow_builders import create_large_rejection_successor
from tests.support.abandonment import Backend, _release, _source
from tests.support.verification import TASK, make_app
from tests.support.abandonment_admin import (
    _released_actor_lease,

)




@pytest.mark.smoke
def test_admin_abandon_operation_creates_exact_planning_successor():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    lease = _released_actor_lease(conn, source["operation_id"])
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute(
        "abandon-operation",
        submission_id=source["operation_id"],
        lease_id=lease["lease_id"],
        reason="the original conversation is permanently unavailable",
    )

    assert result["ok"]
    assert result["allowed_actions"] == ["start"]
    action = result["data"]["required_action"]
    assert action["surface"] == "connected-agent"
    assert action["command"] == "start"
    successor_id = action["arguments"]["prepared_operation_id"]
    assert conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (source["operation_id"],),
    ).fetchone()[:] == ("cancelled", "terminal", "agent_abandoned")
    assert conn.execute(
        "SELECT status,phase,successor_claim_mode FROM operations WHERE operation_id=?",
        (successor_id,),
    ).fetchone()[:] == ("open", "prepare_required", "stage_actor")
    assert conn.execute(
        "SELECT status,current_execution_id FROM abandonment_attempts"
    ).fetchone()[:] == ("awaiting_successor_claim", None)
    assert conn.execute(
        "SELECT command,status FROM operation_executions"
    ).fetchone()[:] == ("abandon-operation", "completed")


@pytest.mark.invariant_abandonment
@pytest.mark.smoke
def test_blocked_abandonment_returns_exact_private_relay_and_fences_agents(tmp_path, monkeypatch):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    lease = _released_actor_lease(conn, source["operation_id"])
    declare_operation_step(conn, source["operation_id"], "unfinished", {"x": 1})
    app = DishAdminApplication(conn, backend=backend)

    blocked = app.execute(
        "abandon-operation",
        submission_id=source["operation_id"],
        lease_id=lease["lease_id"],
        reason="the original conversation is permanently unavailable",
    )
    assert blocked["ok"]
    assert blocked["allowed_actions"] == []
    action = blocked["data"]["required_action"]
    abandonment_id = blocked["data"]["abandonment_id"]
    assert action["surface"] == "private-admin"
    assert action["admin_command"] == (
        f"dish-admin reconcile-abandonment {abandonment_id}"
    )
    assert "wait for confirmation" in action["relay_text"]
    assert "Refresh the authoritative Dish action" in action["after_success"]["instruction"]
    view = app.operation_service.current.authoritative_view(source["operation_id"])
    assert view["legal_actions"] == []
    assert view["required_action"]["admin_command"] == action["admin_command"]
    conn.close()

    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=tmp_path / "honest"),
        backend_factory=lambda: backend,
    )
    monkeypatch.setattr(service, "_assert_mutation_ready", lambda _backend: None)
    monkeypatch.setattr(service, "_release", lambda role=None: _release("planning"))
    attempted = service.execute_agent(
        "prepare",
        {"submission_id": source["operation_id"], "agent": "gpt"},
        principal=ServicePrincipal("owner", "dead-run"),
        request_id=str(uuid.uuid4()),
    )
    assert attempted["code"] == "WRONG_STATE"
    assert attempted["errors"][0]["rule"] == "abandonment_fence_active"
    check = initialize_database(db_path)
    assert LeaseManager(check).active_for_operation(source["operation_id"]) is None
    check.close()


@pytest.mark.smoke
def test_abandonment_requires_latest_released_actor_attempt():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    older = _released_actor_lease(conn, source["operation_id"], run_id="dead-run")
    newer = _released_actor_lease(conn, source["operation_id"], run_id="later-run")
    app = DishAdminApplication(conn, backend=backend)

    rejected = app.execute(
        "abandon-operation",
        submission_id=source["operation_id"],
        lease_id=older["lease_id"],
        reason="old conversation unavailable",
    )
    assert rejected["code"] == "CONFLICT"
    assert rejected["errors"][0]["rule"] == "abandonment_lease_not_eligible"

    accepted = app.execute(
        "abandon-operation",
        submission_id=source["operation_id"],
        lease_id=newer["lease_id"],
        reason="latest conversation unavailable",
    )
    assert accepted["ok"]


@pytest.mark.smoke
def test_cli_parses_abandonment_commands():
    abandon = vars(
        build_parser().parse_args(
            [
                "abandon-operation",
                "operation-id",
                "--lease-id",
                "lease-id",
                "--reason",
                "gone",
            ]
        )
    )
    assert abandon == {
        "command": "abandon-operation",
        "submission_id": "operation-id",
        "reason": "gone",
        "lease_id": "lease-id",
    }
    reconcile = vars(
        build_parser().parse_args(["reconcile-abandonment", "abandonment-id"])
    )
    assert reconcile == {
        "command": "reconcile-abandonment",
        "abandonment_id": "abandonment-id",
    }


@pytest.mark.smoke
def test_crashed_admin_execution_is_reclaimed_and_both_requests_replay(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    lease = _released_actor_lease(conn, source["operation_id"])
    conn.close()

    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=tmp_path / "honest"),
        backend_factory=lambda: backend,
    )
    monkeypatch.setattr(service, "_assert_mutation_ready", lambda _backend: None)
    principal = ServicePrincipal(str(uuid.uuid4()), str(uuid.uuid4()))
    abandon_request = str(uuid.uuid4())
    arguments = {
        "submission_id": source["operation_id"],
        "lease_id": lease["lease_id"],
        "reason": "the original conversation is permanently unavailable",
    }
    original_settle = CurrentWorkflowService.settle_abandonment_frontier

    def crash_after_creation(self, abandonment_id: str, *, reason: str):
        raise SystemExit("simulated process loss after abandonment creation")

    monkeypatch.setattr(
        CurrentWorkflowService, "settle_abandonment_frontier", crash_after_creation
    )
    with pytest.raises(SystemExit):
        service.execute_admin(
            "abandon-operation",
            arguments,
            principal=principal,
            request_id=abandon_request,
        )
    monkeypatch.setattr(
        CurrentWorkflowService, "settle_abandonment_frontier", original_settle
    )

    check = initialize_database(db_path)
    abandonment = check.execute("SELECT * FROM abandonment_attempts").fetchone()
    original_execution_id = abandonment["current_execution_id"]
    assert original_execution_id is not None
    check.execute(
        """UPDATE operation_execution_claims
              SET hostname=?, pid=999999999, process_start='dead'
            WHERE execution_id=?""",
        (socket.gethostname(), original_execution_id),
    )
    check.close()

    reconcile_request = str(uuid.uuid4())
    reconciled = service.execute_admin(
        "reconcile-abandonment",
        {"abandonment_id": abandonment["abandonment_id"]},
        principal=principal,
        request_id=reconcile_request,
    )
    assert reconciled["ok"], reconciled
    assert reconciled["command"] == "reconcile-abandonment"

    replayed_original = service.execute_admin(
        "abandon-operation",
        arguments,
        principal=principal,
        request_id=abandon_request,
    )
    assert replayed_original["ok"]
    assert replayed_original["command"] == "abandon-operation"
    assert replayed_original["data"]["request_replayed"] is True

    final = initialize_database(db_path)
    executions = final.execute(
        "SELECT execution_id,command,status FROM operation_executions ORDER BY created_at"
    ).fetchall()
    assert [row["execution_id"] for row in executions] == [original_execution_id]
    assert executions[0]["command"] == "abandon-operation"
    assert executions[0]["status"] == "completed"
    final.close()


@pytest.mark.smoke
def test_completed_route_continuation_remains_exact_in_authoritative_view(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="dead-verifier-run",
        independence_attestation="independent",
    )
    cycle_id = review["data"]["cycle_id"]
    next_cycle = create_large_rejection_successor(
        app,
        tmp_path,
        operation_id=operation_id,
        agent="codex",
        run_id="dead-verifier-run",
        candidate_text=TASK.replace("100 g", "120 g"),
    )
    from dish_tool.database import create_abandonment_attempt_in_transaction
    lease = LeaseManager(app.conn).acquire(
        operation_id,
        ServicePrincipal("owner", "dead-verifier-run"),
        context_cycle_id=cycle_id,
    )
    LeaseManager(app.conn).release(
        operation_id, None, reason="conversation permanently unavailable", admin=True
    )
    app.conn.execute("BEGIN IMMEDIATE")
    create_abandonment_attempt_in_transaction(
        app.conn,
        abandonment_id="route-abandonment",
        task_gid="t",
        source_operation_id=operation_id,
        source_lease_id=lease["lease_id"],
        abandoned_owner_id="owner",
        abandoned_run_id="dead-verifier-run",
        attempt_cycle_id=cycle_id,
        reason="gone",
    )
    app.conn.execute("COMMIT")
    from dish_tool.abandonment import settle_abandonment_frontier

    settled = settle_abandonment_frontier(
        app.conn,
        _backend,
        abandonment_id="route-abandonment",
        reason="preserve committed route",
    )
    assert settled["required_action"]["arguments"]["target_cycle_id"] == next_cycle["cycle_id"]
    view = app.operation_service.current.authoritative_view(operation_id)
    assert view["legal_actions"] == ["verify"]
    assert view["required_action"]["arguments"] == {
        "task_gid": "t",
        "kind": "verification",
        "target_operation_id": operation_id,
        "target_cycle_id": next_cycle["cycle_id"],
    }
