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
from tests.support.abandonment import Backend, _abandon, _live, _release, _source
from tests.support.abandonment_admin import _released_actor_lease
from tests.support.abandonment_scenarios import (
    abandonment_in_state as _abandonment_in_state,
    count_rows as _count,
)
from tests.support.service_foundation import _release_loader





class RepairBackend(Backend):
    def update_task_content(self, *, task_gid, title, notes):
        assert task_gid == "task"
        self.title = title
        self.notes = notes

    def move_task_to_section(self, *, task_gid, section_gid):
        assert task_gid == "task"
        self.section = section_gid

@pytest.mark.smoke
def test_reconcile_finishes_execution_and_requests_after_post_settlement_crash(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    lease = _released_actor_lease(conn, source["operation_id"])
    conn.close()

    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=honest),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
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
@pytest.mark.smoke
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
@pytest.mark.smoke
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
@pytest.mark.smoke
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
@pytest.mark.smoke
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
