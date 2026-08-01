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
@pytest.mark.smoke
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
@pytest.mark.smoke
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
@pytest.mark.smoke
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
