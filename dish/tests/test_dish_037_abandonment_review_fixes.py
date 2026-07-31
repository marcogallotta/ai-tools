from __future__ import annotations

import socket
import uuid
from dataclasses import replace

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.application_service import CurrentWorkflowService
from dish_tool.database import (
    complete_operation_step,
    create_operation,
    declare_operation_step,
)
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from dish_tool.step5 import claim_prepared_stage_successor
from test_dish_034_abandonment_stage_successors import (
    Backend,
    _abandon,
    _live,
    _release,
    _source,
)
from test_dish_036_abandonment_admin_workflow import _released_actor_lease


def test_active_abandonment_fences_new_operation_after_source_terminalizes():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    _abandon(conn, source)
    conn.execute(
        """UPDATE operations
              SET status='completed', phase='terminal',
                  terminal_outcome='planning_handoff', completed_at='now'
            WHERE operation_id=?""",
        (source["operation_id"],),
    )

    with pytest.raises(DishRuleError) as raised:
        create_operation(
            conn,
            task_gid="task",
            operation_kind="planning",
            expected_identity=source["expected_identity"],
            schema_version="2",
            expected_section_gid="pi",
            actors=OperationActors(editor_agent="gpt", run_id="new-run"),
        )

    assert raised.value.rule == "abandonment_fence_active"
    assert conn.execute(
        "SELECT COUNT(*) FROM operations WHERE task_gid='task'"
    ).fetchone()[0] == 1


def test_service_blocks_ordinary_start_before_backend_while_task_is_fenced(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    _abandon(conn, source)
    conn.execute(
        """UPDATE operations
              SET status='completed', phase='terminal',
                  terminal_outcome='planning_handoff', completed_at='now'
            WHERE operation_id=?""",
        (source["operation_id"],),
    )
    conn.close()

    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=tmp_path / "honest"),
        backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("task fence must run before backend construction")
        ),
    )
    result = service.execute_agent(
        "start",
        {"task_gid": "task", "agent": "gpt", "kind": "planning"},
        principal=ServicePrincipal("owner", "new-run"),
        request_id=str(uuid.uuid4()),
    )

    assert result["code"] == "WRONG_STATE"
    assert result["errors"][0]["rule"] == "abandonment_fence_active"
    check = initialize_database(db_path)
    assert check.execute(
        "SELECT COUNT(*) FROM operations WHERE task_gid='task'"
    ).fetchone()[0] == 1
    check.close()


def test_reconcile_finishes_execution_and_requests_after_post_settlement_crash(
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

    import dish_tool.application_service as application_service

    original_finish = application_service.finish_operation_execution

    def crash_before_execution_completion(*_args, **_kwargs):
        raise SystemExit("simulated process loss after abandonment settlement")

    monkeypatch.setattr(
        application_service,
        "finish_operation_execution",
        crash_before_execution_completion,
    )
    with pytest.raises(SystemExit):
        service.execute_admin(
            "abandon-operation",
            arguments,
            principal=principal,
            request_id=abandon_request,
        )
    monkeypatch.setattr(
        application_service, "finish_operation_execution", original_finish
    )

    check = initialize_database(db_path)
    abandonment = check.execute("SELECT * FROM abandonment_attempts").fetchone()
    execution = check.execute("SELECT * FROM operation_executions").fetchone()
    request = check.execute(
        "SELECT * FROM service_requests WHERE request_id=?", (abandon_request,)
    ).fetchone()
    assert abandonment["status"] == "awaiting_successor_claim"
    assert abandonment["current_execution_id"] is None
    assert execution["status"] == "started"
    assert request["status"] == "pending"
    check.execute(
        """UPDATE operation_execution_claims
              SET hostname=?, pid=999999999, process_start='dead'
            WHERE execution_id=?""",
        (socket.gethostname(), execution["execution_id"]),
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

    final = initialize_database(db_path)
    assert final.execute(
        "SELECT status FROM operation_executions WHERE execution_id=?",
        (execution["execution_id"],),
    ).fetchone()[0] == "completed"
    assert final.execute(
        "SELECT COUNT(*) FROM operation_execution_claims"
    ).fetchone()[0] == 0
    assert final.execute(
        "SELECT status FROM service_requests WHERE request_id=?",
        (abandon_request,),
    ).fetchone()[0] == "completed"
    assert final.execute(
        "SELECT status FROM service_requests WHERE request_id=?",
        (reconcile_request,),
    ).fetchone()[0] == "completed"
    final.close()


def test_prepared_stage_successor_adopts_current_schema_at_claim():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    _abandon(conn, source)
    prepared = CurrentWorkflowService(conn, backend).settle_abandonment_frontier(
        "abandonment", reason="conversation permanently unavailable"
    )
    successor_id = prepared["successor_operation_id"]
    current_release = replace(_release("planning"), schema_version="3")

    claimed = claim_prepared_stage_successor(
        conn,
        live=_live(backend),
        release=current_release,
        kind="planning",
        agent="gpt",
        run_id="fresh-run",
        prepared_operation_id=successor_id,
    )

    assert claimed["schema_version"] == "3"
    audit = conn.execute(
        """SELECT details FROM audit_events
             WHERE operation_id=? AND event_type='operation.successor_claimed'""",
        (successor_id,),
    ).fetchone()
    assert '"previous_schema_version":"2"' in audit["details"]
    assert '"claimed_schema_version":"3"' in audit["details"]


def test_abandoned_hold_relay_includes_generated_command_template():
    conn = initialize_database(":memory:")
    backend = Backend(section="rq")
    source = _source(conn, backend, kind="initial", phase="held_evidence")
    declare_operation_step(
        conn,
        source["operation_id"],
        "research_preconstruction_hold",
        {
            "route": "evidence",
            "resume_status": "pending-research",
            "candidate_content_existed": False,
        },
    )
    complete_operation_step(
        conn, source["operation_id"], "research_preconstruction_hold"
    )
    lease = _released_actor_lease(conn, source["operation_id"])

    result = DishAdminApplication(conn, backend=backend).execute(
        "abandon-operation",
        submission_id=source["operation_id"],
        lease_id=lease["lease_id"],
        reason="the original conversation is permanently unavailable",
    )

    action = result["data"]["required_action"]
    template = action["admin_command_template"]
    assert action["admin_command"] is None
    assert template.startswith("dish-admin supply-evidence")
    assert template in action["relay_text"]
    assert "replacing the angle-bracketed detail text" in action["relay_text"]
