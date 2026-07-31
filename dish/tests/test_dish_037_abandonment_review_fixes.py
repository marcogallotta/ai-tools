from __future__ import annotations

import socket
import sqlite3
import uuid
from dataclasses import replace

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.abandonment import settle_abandonment_frontier
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


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _abandonment_in_state(conn, backend, *, target_status: str) -> sqlite3.Row:
    """Drive a fresh abandonment on task "task" to exactly `target_status`."""
    if target_status == "blocked_manual_reconciliation":
        source = _source(conn, backend, kind="initial")
        declare_operation_step(
            conn,
            source["operation_id"],
            "candidate_write",
            {"title": "Changed", "notes": "changed", "schema_version": "2"},
        )
        _abandon(conn, source)
        settle_abandonment_frontier(
            conn, backend, abandonment_id="abandonment", reason="gone"
        )
    elif target_status == "awaiting_hold_resolution":
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
        _abandon(conn, source)
        settle_abandonment_frontier(
            conn, backend, abandonment_id="abandonment", reason="gone"
        )
    elif target_status == "awaiting_successor_claim":
        source = _source(conn, backend, kind="planning")
        _abandon(conn, source)
        settle_abandonment_frontier(
            conn, backend, abandonment_id="abandonment", reason="gone"
        )
    else:
        raise ValueError(target_status)

    stored_status = conn.execute(
        "SELECT status FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[0]
    assert stored_status == target_status
    return source


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


@pytest.mark.parametrize(
    "target_status",
    [
        "blocked_manual_reconciliation",
        "awaiting_hold_resolution",
    ],
)
def test_active_abandonment_fences_new_operation_across_all_active_states(
    tmp_path, target_status
):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi")
    source = _abandonment_in_state(conn, backend, target_status=target_status)

    baseline_operations = _count(conn, "operations")
    baseline_leases = _count(conn, "service_leases")
    baseline_actor_facts = _count(conn, "operation_actor_facts")

    with pytest.raises(DishRuleError) as raised:
        create_operation(
            conn,
            task_gid="task",
            operation_kind="planning",
            expected_identity=source["expected_identity"],
            schema_version="2",
            expected_section_gid=backend.section,
            actors=OperationActors(editor_agent="gpt", run_id="new-run"),
        )
    assert raised.value.rule == "abandonment_fence_active"
    assert _count(conn, "operations") == baseline_operations
    assert _count(conn, "service_leases") == baseline_leases
    assert _count(conn, "operation_actor_facts") == baseline_actor_facts
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
    assert _count(check, "operations") == baseline_operations
    assert _count(check, "service_leases") == baseline_leases
    assert _count(check, "operation_actor_facts") == baseline_actor_facts
    check.close()


def test_awaiting_successor_claim_lets_plain_start_through_to_resolve(tmp_path):
    """A ready prepared successor is no longer a fence for a plain start.

    Unlike the still-blocked states above, `awaiting_successor_claim` means a
    successor is prepared and ready; a caller that names only the task_gid
    passes the connected-surface fence and reaches ordinary Planning `start`
    handling (here, its own two-call intent gate), which resolves the exact
    prepared successor itself rather than requiring the caller to echo it.
    """
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi")
    _abandonment_in_state(conn, backend, target_status="awaiting_successor_claim")
    conn.close()

    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=tmp_path / "honest"),
        backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("Planning's own intent gate must run before backend construction")
        ),
    )
    result = service.execute_agent(
        "start",
        {"task_gid": "task", "agent": "gpt", "kind": "planning"},
        principal=ServicePrincipal("owner", "new-run"),
        request_id=str(uuid.uuid4()),
    )

    assert result["code"] == "CONFIRMATION_REQUIRED"
    assert result["errors"][0]["rule"] == "planning_intent_confirmation_required"


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
    assert source["operation_id"] in template
    assert "--resume-status pending-research" in template
    assert template in action["relay_text"]
    assert "replacing the angle-bracketed detail text" in action["relay_text"]


def test_abandoned_human_review_hold_relay_includes_generated_command_template():
    conn = initialize_database(":memory:")
    backend = Backend(section="rq")
    source = _source(conn, backend, kind="initial", phase="held_human")
    declare_operation_step(
        conn,
        source["operation_id"],
        "research_preconstruction_hold",
        {
            "route": "human-review",
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
    assert template.startswith("dish-admin record-human-decision")
    assert source["operation_id"] in template
    assert "--resume-status pending-research" in template
    assert template in action["relay_text"]
    assert "replacing the angle-bracketed detail text" in action["relay_text"]


class RepairBackend(Backend):
    def update_task_content(self, *, task_gid, title, notes):
        assert task_gid == "task"
        self.title = title
        self.notes = notes

    def move_task_to_section(self, *, task_gid, section_gid):
        assert task_gid == "task"
        self.section = section_gid


@pytest.mark.parametrize(
    ("drift_content", "drift_section"),
    [(True, False), (False, True), (True, True)],
)
def test_prepared_stage_drift_blocks_then_reconcile_restores_exact_target(
    drift_content, drift_section
):
    conn = initialize_database(":memory:")
    backend = RepairBackend(title="Original", notes="baseline", section="pi")
    source = _source(conn, backend, kind="planning")
    _abandon(conn, source)
    prepared = CurrentWorkflowService(conn, backend).settle_abandonment_frontier(
        "abandonment", reason="conversation permanently unavailable"
    )
    successor_id = prepared["successor_operation_id"]

    if drift_content:
        backend.title = "Externally edited"
        backend.notes = "different live content"
    if drift_section:
        backend.section = "rq"

    with pytest.raises(DishRuleError) as raised:
        claim_prepared_stage_successor(
            conn,
            live=_live(backend),
            release=_release("planning"),
            kind="planning",
            agent="gpt",
            run_id="fresh-run",
            prepared_operation_id=successor_id,
        )

    assert raised.value.rule == "prepared_successor_drift"
    blocked = conn.execute(
        "SELECT * FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()
    assert blocked["status"] == "blocked_manual_reconciliation"
    assert blocked["successor_operation_id"] == successor_id

    reconciled = DishAdminApplication(conn, backend=backend).execute(
        "reconcile-abandonment", abandonment_id="abandonment"
    )
    assert reconciled["ok"], reconciled
    action = reconciled["data"]["required_action"]
    assert action["command"] == "start"
    assert action["arguments"]["prepared_operation_id"] == successor_id
    assert backend.title == "Original"
    assert backend.notes == "baseline"
    assert backend.section == "pi"
    awaiting = conn.execute(
        "SELECT * FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()
    assert awaiting["status"] == "awaiting_successor_claim"

    claimed = claim_prepared_stage_successor(
        conn,
        live=_live(backend),
        release=_release("planning"),
        kind="planning",
        agent="gpt",
        run_id="fresh-run",
        prepared_operation_id=successor_id,
    )
    assert claimed["run_id"] == "fresh-run"
    assert conn.execute(
        "SELECT status FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[0] == "completed"
