from __future__ import annotations

import sqlite3

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


PLANNING_NOTES = """### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
"""


class Backend:
    def __init__(self, *, title: str, notes: str, section: str):
        self.title = title
        self.notes = notes
        self.section = section
        self.sections = [
            {"gid": "pi", "name": "Planning (Incomplete)"},
            {"gid": "rq", "name": "Research Queue"},
            {"gid": "vq", "name": "Verification Queue"},
            {"gid": "12345", "name": "Sichuan"},
            {"gid": "src", "name": "Sourcing"},
            {"gid": "ref", "name": "Reference"},
        ]

    def list_sections(self, project_gid):
        assert project_gid == COOKING_PROJECT_GID
        return self.sections

    def read_task(self, gid):
        return {
            "gid": gid,
            "name": self.title,
            "notes": self.notes,
            "completed": False,
            "modified_at": "now",
            "projects": [{"gid": COOKING_PROJECT_GID}],
            "memberships": [
                {
                    "project": {"gid": COOKING_PROJECT_GID},
                    "section": {"gid": self.section},
                }
            ],
        }

    def update_task_content(self, *, task_gid, title, notes):
        raise AssertionError("Stage 4 must not perform an external content write")

    def move_task_to_section(self, *, task_gid, section_gid):
        raise AssertionError("Stage 4 must not perform an external movement")


def _operation(
    conn: sqlite3.Connection,
    backend: Backend,
    *,
    kind: str,
    phase: str = "prepare_required",
    run_id: str = "dead-run",
):
    baseline = confirm_task_content(
        conn,
        task_gid="task",
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
    )
    row = create_operation(
        conn,
        task_gid="task",
        operation_kind=kind,
        expected_identity=baseline.digest,
        schema_version="2",
        expected_section_gid=backend.section,
        actors=OperationActors(
            editor_agent="gpt",
            researcher_agent="gpt",
            run_id=run_id,
        ),
    )
    if phase != "prepare_required":
        conn.execute(
            "UPDATE operations SET phase=? WHERE operation_id=?",
            (phase, row["operation_id"]),
        )
    return conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (row["operation_id"],)
    ).fetchone()


def _abandonment(
    conn: sqlite3.Connection,
    operation: sqlite3.Row,
    *,
    cycle: sqlite3.Row | None = None,
    abandonment_id: str = "abandonment",
):
    lease = LeaseManager(conn).acquire(
        operation["operation_id"],
        ServicePrincipal("owner", "dead-run"),
        context_cycle_id=None if cycle is None else cycle["cycle_id"],
    )
    LeaseManager(conn).release(
        operation["operation_id"], None, reason="stale actor released", admin=True
    )
    conn.execute("BEGIN IMMEDIATE")
    row = create_abandonment_attempt_in_transaction(
        conn,
        abandonment_id=abandonment_id,
        task_gid=operation["task_gid"],
        source_operation_id=operation["operation_id"],
        source_lease_id=lease["lease_id"],
        abandoned_owner_id="owner",
        abandoned_run_id="dead-run",
        attempt_cycle_id=None if cycle is None else cycle["cycle_id"],
        reason="chat permanently unavailable",
    )
    conn.execute("COMMIT")
    return row


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


def test_confirmed_research_handoff_preserves_exact_verification_continuation():
    conn = initialize_database(":memory:")
    backend = Backend(title="Candidate", notes="candidate", section="rq")
    operation = _operation(conn, backend, kind="initial")

    backend.section = "vq"
    confirm_task_content(
        conn,
        task_gid="task",
        operation_id=operation["operation_id"],
        boundary="content_write",
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
    )
    intents = {
        "candidate_write": {
            "title": backend.title,
            "notes": backend.notes,
            "schema_version": "2",
        },
        "handoff_validation": {
            "title": backend.title,
            "notes": backend.notes,
            "schema_version": "2",
            "schema": {},
        },
        "verification_cycle": {
            "protocol_release": "verification-v1",
            "protocol_text": "protocol",
        },
        "verification_handoff": {"section_gid": "vq"},
        "verification_phase": {"phase": "await_verification", "status": "open"},
    }
    for name, intended in intents.items():
        declare_operation_step(conn, operation["operation_id"], name, intended)
    for name in (
        "candidate_write",
        "handoff_validation",
        "verification_cycle",
        "verification_handoff",
    ):
        complete_operation_step(conn, operation["operation_id"], name)
    cycle = create_verification_cycle(
        conn,
        operation_id=operation["operation_id"],
        task_gid="task",
        cycle_number=1,
        protocol_release="verification-v1",
        protocol_text="protocol",
    )
    _abandonment(conn, operation)

    result = settle_abandonment_frontier(
        conn,
        backend,
        abandonment_id="abandonment",
        reason="finish confirmed Research handoff",
    )

    assert result["continuation_operation_id"] == operation["operation_id"]
    assert result["continuation_cycle_id"] == cycle["cycle_id"]
    assert result["abandonment"]["outcome"] == "route_preserved"
    source = conn.execute(
        "SELECT status,phase FROM operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()
    assert tuple(source) == ("open", "await_verification")


def test_completed_rejection_route_preserves_existing_unbound_cycle_exactly():
    conn = initialize_database(":memory:")
    backend = Backend(title="Candidate", notes="candidate", section="vq")
    operation = _operation(conn, backend, kind="initial", phase="await_verification")
    old_cycle = create_verification_cycle(
        conn,
        operation_id=operation["operation_id"],
        task_gid="task",
        cycle_number=1,
        protocol_release="verification-v1",
        protocol_text="protocol",
        verifier_agent="claude",
        run_id="dead-run",
        independence_attestation="independent",
    )
    conn.execute(
        """UPDATE verification_cycles
              SET outcome='rejected', correction_class='large', completed_at='now'
            WHERE cycle_id=?""",
        (old_cycle["cycle_id"],),
    )
    next_cycle = create_verification_cycle(
        conn,
        operation_id=operation["operation_id"],
        task_gid="task",
        cycle_number=2,
        protocol_release="verification-v1",
        protocol_text="protocol",
    )
    _abandonment(conn, operation, cycle=old_cycle)

    result = settle_abandonment_frontier(
        conn,
        backend,
        abandonment_id="abandonment",
        reason="preserve committed rejection route",
    )

    assert result["continuation_operation_id"] == operation["operation_id"]
    assert result["continuation_cycle_id"] == next_cycle["cycle_id"]
    assert result["abandonment"]["outcome"] == "route_preserved"
    assert conn.execute(
        "SELECT outcome FROM verification_cycles WHERE cycle_id=?",
        (old_cycle["cycle_id"],),
    ).fetchone()[0] == "rejected"


def test_applied_large_rejection_suffix_recovers_without_repeating_external_write(
    tmp_path, monkeypatch
):
    import dish_tool.step8 as step8

    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="dead-run",
        independence_attestation="independent",
    )
    assert review["ok"]
    assert app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )["ok"]

    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))
    original_create = step8.create_verification_cycle

    def crash_before_new_cycle(*args, **kwargs):
        raise RuntimeError("crash before new cycle")

    monkeypatch.setattr(step8, "create_verification_cycle", crash_before_new_cycle)
    failed = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="replace method",
        file_path=str(candidate),
        run_id="dead-run",
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    monkeypatch.setattr(step8, "create_verification_cycle", original_create)

    source_cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND run_id='dead-run'",
        (operation_id,),
    ).fetchone()
    operation = app.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    _abandonment(app.conn, operation, cycle=source_cycle)
    writes_before = backend.writes
    moves_before = backend.moves

    result = settle_abandonment_frontier(
        app.conn,
        backend,
        abandonment_id="abandonment",
        reason="finish applied rejection route",
    )

    assert result["abandonment"]["outcome"] == "route_preserved"
    assert result["continuation_operation_id"] == operation_id
    assert result["continuation_cycle_id"]
    assert backend.writes == writes_before
    assert backend.moves == moves_before
    assert app.conn.execute(
        "SELECT outcome FROM verification_cycles WHERE cycle_id=?",
        (source_cycle["cycle_id"],),
    ).fetchone()[0] == "rejected"
    continuation = app.conn.execute(
        "SELECT run_id, verifier_agent FROM verification_cycles WHERE cycle_id=?",
        (result["continuation_cycle_id"],),
    ).fetchone()
    assert tuple(continuation) == (None, None)
