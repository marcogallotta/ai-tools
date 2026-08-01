from __future__ import annotations

import sqlite3

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.abandonment import (
    classify_abandonment_frontier,
    settle_abandonment_frontier,
)
from dish_tool.application_service import CurrentWorkflowService
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import (
    complete_operation_step,
    confirm_task_content,
    create_abandonment_attempt_in_transaction,
    create_operation,
    create_verification_cycle,
    declare_operation_step,
)
from dish_tool.database_schema import initialize_database
from dish_tool.models import OperationActors, ResolvedRelease
from tests.support.verification import TASK, make_app
from tests.support.abandonment_scenarios import (
    PLANNING_NOTES,
    FrontierBackend as Backend,
    frontier_abandonment as _abandonment,
    frontier_operation as _operation,
)









def test_clean_planning_frontier_is_restart_prepared_without_mutation():
    conn = initialize_database(":memory:")
    backend = Backend(title="Bare", notes="", section="pi")
    operation = _operation(conn, backend, kind="planning")
    _abandonment(conn, operation)

    frontier = CurrentWorkflowService(conn, backend).classify_abandonment(
        "abandonment"
    )

    assert frontier.outcome == "restart_prepared"
    assert frontier.stage == "planning"
    assert frontier.source_content_version_id
    assert conn.execute(
        "SELECT status FROM operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()[0] == "open"
def test_preconstruction_research_hold_is_preserved_and_fenced():
    conn = initialize_database(":memory:")
    backend = Backend(title="Bare", notes="", section="rq")
    operation = _operation(conn, backend, kind="initial", phase="held_evidence")
    declare_operation_step(
        conn,
        operation["operation_id"],
        "research_preconstruction_hold",
        {"route": "evidence", "resume_status": "pending-research"},
    )
    complete_operation_step(
        conn, operation["operation_id"], "research_preconstruction_hold"
    )
    _abandonment(conn, operation)

    result = CurrentWorkflowService(conn, backend).settle_abandonment_frontier(
        "abandonment", reason="chat permanently unavailable"
    )

    assert result["classification"]["outcome"] == "awaiting_hold_resolution"
    stored = conn.execute(
        "SELECT status,outcome FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()
    assert tuple(stored) == ("awaiting_hold_resolution", "hold_preserved")
    assert conn.execute(
        "SELECT phase FROM operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()[0] == "held_evidence"
def test_pending_unapplied_step_blocks_without_source_mutation():
    conn = initialize_database(":memory:")
    backend = Backend(title="Bare", notes="", section="rq")
    operation = _operation(conn, backend, kind="initial")
    declare_operation_step(
        conn,
        operation["operation_id"],
        "candidate_write",
        {"title": "Changed", "notes": "changed", "schema_version": "2"},
    )
    _abandonment(conn, operation)

    result = settle_abandonment_frontier(
        conn,
        backend,
        abandonment_id="abandonment",
        reason="chat permanently unavailable",
    )

    assert result["classification"]["outcome"] == "blocked_manual_reconciliation"
    assert conn.execute(
        "SELECT status FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[0] == "blocked_manual_reconciliation"
    assert conn.execute(
        "SELECT status FROM operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()[0] == "open"
def test_confirmed_planning_handoff_finishes_existing_recovery_suffix():
    conn = initialize_database(":memory:")
    backend = Backend(title="Bare", notes="", section="pi")
    operation = _operation(conn, backend, kind="planning")

    backend.notes = PLANNING_NOTES
    backend.section = "rq"
    confirm_task_content(
        conn,
        task_gid="task",
        operation_id=operation["operation_id"],
        boundary="planning_write",
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
    )
    declare_operation_step(
        conn,
        operation["operation_id"],
        "planning_write",
        {"title": backend.title, "notes": backend.notes, "schema_version": "2"},
    )
    declare_operation_step(
        conn,
        operation["operation_id"],
        "planning_handoff",
        {"section_gid": "rq"},
    )
    declare_operation_step(
        conn,
        operation["operation_id"],
        "planning_terminal",
        {
            "status": "completed",
            "phase": "terminal",
            "terminal_outcome": "planning_handoff_confirmed",
        },
    )
    complete_operation_step(conn, operation["operation_id"], "planning_write")
    complete_operation_step(conn, operation["operation_id"], "planning_handoff")
    _abandonment(conn, operation)

    frontier = classify_abandonment_frontier(
        conn, backend, abandonment_id="abandonment"
    )
    assert frontier.outcome == "committed_finalized"
    assert frontier.recovery_required is True

    result = settle_abandonment_frontier(
        conn,
        backend,
        abandonment_id="abandonment",
        reason="finish confirmed Planning handoff",
    )

    assert result["abandonment"]["outcome"] == "committed_finalized"
    source = conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()
    assert tuple(source) == (
        "completed",
        "terminal",
        "planning_handoff_confirmed",
    )
@pytest.mark.producer_equivalence
def test_real_planning_prepare_crash_before_terminal_preserves_committed_finalized_route(
    tmp_path, monkeypatch
):
    """Producer-contract companion to test_confirmed_planning_handoff_finishes_existing_recovery_suffix.

    Drives the real "start"+"prepare" command path (dish_tool.step6.prepare_live)
    for a Planning handoff and crashes it at the exact same point (after the
    planning_write/planning_handoff steps commit, before transition_operation
    completes planning_terminal) instead of hand-declaring that step shape,
    proving the real producer leaves the state the fabricated-state test assumes.
    """
    from dish_tool import step6
    from dish_tool.commands import DishApplication

    class WritableBackend(Backend):
        def __init__(self):
            super().__init__(title="Bare", notes="", section="pi")
            self.writes = 0
            self.moves = 0

        def update_task_content(self, *, task_gid, title, notes):
            self.writes += 1
            self.title, self.notes = title, notes

        def move_task_to_section(self, *, task_gid, section_gid):
            self.moves += 1
            self.section = section_gid

    backend = WritableBackend()
    honest = tmp_path / "honest"
    honest.mkdir()

    def release(role=None):
        return ResolvedRelease(
            version="test-release", commit="test", root=honest,
            protocols={} if role is None else {role: f"{role} protocol"},
            manifests={}, manifest_texts={}, schema_version="2", schema={},
            schema_text="{}", requested_protocol_role=role,
        )

    app = DishApplication(
        initialize_database(tmp_path / "dish.db"), backend, release_loader=release
    )
    started = app.execute(
        "start", agent="gpt", task_gid="task", kind="planning",
        change_level=None, change_reason=None, run_id="dead-run",
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    candidate = tmp_path / "planning.txt"
    candidate.write_text(PLANNING_NOTES)

    original_transition = step6.transition_operation

    def crash_before_terminal(*args, **kwargs):
        raise RuntimeError("crash before planning terminal")

    monkeypatch.setattr(step6, "transition_operation", crash_before_terminal)
    failed = app.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=operation_id, file_path=str(candidate),
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["data"]["failed_step"] == "planning_terminal"
    monkeypatch.setattr(step6, "transition_operation", original_transition)

    operation = app.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    assert operation["status"] == "open"
    _abandonment(app.conn, operation)

    frontier = classify_abandonment_frontier(
        app.conn, backend, abandonment_id="abandonment"
    )
    assert frontier.outcome == "committed_finalized"
    assert frontier.recovery_required is True

    result = settle_abandonment_frontier(
        app.conn, backend, abandonment_id="abandonment",
        reason="finish confirmed Planning handoff",
    )

    assert result["abandonment"]["outcome"] == "committed_finalized"
    source = app.conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert tuple(source) == ("completed", "terminal", "planning_handoff_confirmed")
def test_completed_recovery_is_bookkept_after_process_loss_without_repeating():
    conn = initialize_database(":memory:")
    backend = Backend(title="Bare", notes="", section="pi")
    operation = _operation(conn, backend, kind="planning")

    backend.notes = PLANNING_NOTES
    backend.section = "rq"
    confirm_task_content(
        conn,
        task_gid="task",
        operation_id=operation["operation_id"],
        boundary="planning_write",
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
    )
    for name, intended in {
        "planning_write": {
            "title": backend.title,
            "notes": backend.notes,
            "schema_version": "2",
        },
        "planning_handoff": {"section_gid": "rq"},
        "planning_terminal": {
            "status": "completed",
            "phase": "terminal",
            "terminal_outcome": "planning_handoff_confirmed",
        },
    }.items():
        declare_operation_step(conn, operation["operation_id"], name, intended)
    complete_operation_step(conn, operation["operation_id"], "planning_write")
    complete_operation_step(conn, operation["operation_id"], "planning_handoff")
    _abandonment(conn, operation)

    # Simulate process loss after the existing recovery suffix committed but
    # before the abandonment command recorded its result.
    from dish_tool.step9 import recover_operation

    recover_operation(
        conn,
        backend,
        operation_id=operation["operation_id"],
        requested_outcome="applied",
        reason="finish confirmed Planning handoff",
    )
    assert conn.execute(
        "SELECT status FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[0] == "started"

    result = settle_abandonment_frontier(
        conn,
        backend,
        abandonment_id="abandonment",
        reason="resume abandonment bookkeeping",
    )

    assert result["classification"]["recovery_required"] is False
    assert result["abandonment"]["outcome"] == "committed_finalized"
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_events "
        "WHERE operation_id=? AND event_type='operation.recovery'",
        (operation["operation_id"],),
    ).fetchone()[0] == 1
