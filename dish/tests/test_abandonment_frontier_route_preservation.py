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
from dish_tool.database_initialization import initialize_database
from dish_tool.models import OperationActors, ResolvedRelease
from tests.support.verification import TASK, make_app
from tests.support.abandonment_scenarios import (
    PLANNING_NOTES,
    FrontierBackend as Backend,
    frontier_abandonment as _abandonment,
    frontier_operation as _operation,
)









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
