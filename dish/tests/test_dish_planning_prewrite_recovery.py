from __future__ import annotations

import pytest

from dish_tool.database import (
    complete_operation_step,
    content_identity,
    declare_operation_step,
)
from dish_tool.errors import DishRuleError
from dish_tool.recovery import begin_movement_attempt, begin_operation_write_attempt
from dish_tool.step9 import recover_operation
from tests.test_dish_tool_step6_prepare import Backend, PLANNING, app, write


def _nbsp_duplicate_planning() -> str:
    return PLANNING.replace(
        "Purpose: Compare texture\n",
        "Purpose: Compare texture\nPurpose\u00a0: Compare aroma\n",
    )


def _started_planning(tmp_path, *, section: str = "12345"):
    backend = Backend(section=section)
    application = app(tmp_path, backend)
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="planning",
        change_level=None,
        change_reason=None,
    )
    assert started["ok"]
    return backend, application, started["submission_id"]


def _declare_planning_suffix(application, operation_id: str, notes: str) -> None:
    declare_operation_step(
        application.conn,
        operation_id,
        "planning_write",
        {"title": "Bare", "notes": notes, "schema_version": "2"},
    )
    declare_operation_step(
        application.conn,
        operation_id,
        "planning_handoff",
        {"section_gid": "rq"},
    )
    declare_operation_step(
        application.conn,
        operation_id,
        "planning_terminal",
        {
            "status": "completed",
            "phase": "terminal",
            "terminal_outcome": "planning_handoff_confirmed",
        },
    )


def test_nbsp_hidden_planning_label_fails_before_any_write_intent(tmp_path):
    backend, application, operation_id = _started_planning(tmp_path)
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=write(tmp_path, "nbsp-hidden.txt", _nbsp_duplicate_planning()),
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0] == {
        "rule": "planning_field_duplicate",
        "field": "Purpose",
        "occurrences": 2,
        "lines": [3, 4],
        "message": "duplicate planning field Purpose",
    }
    assert backend.writes == 0
    assert backend.moves == 0
    assert application.conn.execute(
        "SELECT COUNT(*) FROM operation_steps WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 0
    assert application.conn.execute(
        "SELECT COUNT(*) FROM write_attempts WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 0


def test_recovery_does_not_confirm_malformed_planning_write(tmp_path):
    backend, application, operation_id = _started_planning(tmp_path)
    notes = _nbsp_duplicate_planning()
    _declare_planning_suffix(application, operation_id, notes)
    operation = application.conn.execute(
        "SELECT expected_identity, schema_version FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    attempt_id = begin_operation_write_attempt(
        application.conn,
        operation_id=operation_id,
        expected_identity=operation["expected_identity"],
        intended_identity=content_identity("Bare", notes).digest,
        intended_title="Bare",
        intended_notes=notes,
        schema_version=operation["schema_version"],
    )
    backend.notes = notes

    with pytest.raises(DishRuleError) as exc:
        recover_operation(
            application.conn,
            backend,
            operation_id=operation_id,
            requested_outcome="applied",
            reason="resume interrupted Planning write",
        )
    assert exc.value.rule == "planning_recovery_validation_failed"
    assert application.conn.execute(
        "SELECT outcome FROM write_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()[0] == "started"
    assert application.conn.execute(
        "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='planning_write'",
        (operation_id,),
    ).fetchone()[0] is None
    assert backend.moves == 0


def test_recovery_does_not_move_malformed_planning_content(tmp_path):
    backend, application, operation_id = _started_planning(tmp_path)
    notes = _nbsp_duplicate_planning()
    _declare_planning_suffix(application, operation_id, notes)
    complete_operation_step(application.conn, operation_id, "planning_write")
    backend.notes = notes

    with pytest.raises(DishRuleError) as exc:
        recover_operation(
            application.conn,
            backend,
            operation_id=operation_id,
            requested_outcome="applied",
            reason="resume interrupted Planning handoff",
        )
    assert exc.value.rule == "planning_recovery_validation_failed"
    assert backend.section == "12345"
    assert backend.moves == 0
    assert application.conn.execute(
        "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='planning_handoff'",
        (operation_id,),
    ).fetchone()[0] is None


def test_recovery_does_not_finalize_malformed_planning_movement(tmp_path):
    backend, application, operation_id = _started_planning(tmp_path)
    notes = _nbsp_duplicate_planning()
    _declare_planning_suffix(application, operation_id, notes)
    complete_operation_step(application.conn, operation_id, "planning_write")
    backend.notes = notes
    attempt_id = begin_movement_attempt(
        application.conn,
        operation_id=operation_id,
        expected_section_gid="12345",
        intended_section_gid="rq",
        purpose="planning_handoff",
    )
    backend.section = "rq"

    with pytest.raises(DishRuleError) as exc:
        recover_operation(
            application.conn,
            backend,
            operation_id=operation_id,
            requested_outcome="applied",
            reason="resume interrupted Planning movement",
        )
    assert exc.value.rule == "planning_recovery_validation_failed"
    assert application.conn.execute(
        "SELECT outcome FROM movement_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()[0] == "started"
    assert application.conn.execute(
        "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='planning_handoff'",
        (operation_id,),
    ).fetchone()[0] is None


def test_recovery_does_not_complete_malformed_planning_terminal(tmp_path):
    backend, application, operation_id = _started_planning(tmp_path)
    notes = _nbsp_duplicate_planning()
    _declare_planning_suffix(application, operation_id, notes)
    complete_operation_step(application.conn, operation_id, "planning_write")
    complete_operation_step(application.conn, operation_id, "planning_handoff")
    backend.notes = notes
    backend.section = "rq"

    with pytest.raises(DishRuleError) as exc:
        recover_operation(
            application.conn,
            backend,
            operation_id=operation_id,
            requested_outcome="applied",
            reason="resume interrupted Planning terminal",
        )
    assert exc.value.rule == "planning_recovery_validation_failed"
    operation = application.conn.execute(
        "SELECT status, phase, terminal_outcome FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert tuple(operation) == ("open", "prepare_required", None)
    assert application.conn.execute(
        "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='planning_terminal'",
        (operation_id,),
    ).fetchone()[0] is None
