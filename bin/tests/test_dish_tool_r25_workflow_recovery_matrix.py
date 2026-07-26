from pathlib import Path

import pytest

from dish_tool import step7, step8
from dish_tool.step9 import recover_operation
from test_dish_tool_step7_verification import TASK, make_app


def _review(app, agent="codex", run="review"):
    result = app.execute("start", agent=agent, task_gid="t", kind="verification", run_id=run)
    assert result["ok"]
    return result


def test_approval_crash_after_signoff_recovers_await_submission(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = _review(app)
    original_transition = step7.transition_operation

    def crash_before_phase(*args, **kwargs):
        raise RuntimeError("crash after signoff")

    monkeypatch.setattr(step7, "transition_operation", crash_before_phase)
    result = app.execute(
        "approve",
        agent="codex",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="review",
    )
    assert result["code"] == "INTERNAL_ERROR"
    row = app.conn.execute("SELECT phase, signoff_completed_at FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert row["phase"] == "await_verification"
    assert row["signoff_completed_at"] is not None

    monkeypatch.setattr(step7, "transition_operation", original_transition)
    recovered = recover_operation(app.conn, backend, operation_id=operation_id, requested_outcome="applied")
    assert any(action.get("step") == "signoff_finalize" for action in recovered["actions"])
    assert app.conn.execute("SELECT phase FROM operations WHERE operation_id=?", (operation_id,)).fetchone()[0] == "await_submission"


def test_large_crash_before_new_cycle_recovers_missing_suffix(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app, run="first")
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))
    original_create = step8.create_verification_cycle

    def crash_new_cycle(*args, **kwargs):
        raise RuntimeError("crash before new cycle")

    monkeypatch.setattr(step8, "create_verification_cycle", crash_new_cycle)
    result = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="large",
        reason="replace method",
        file_path=str(candidate),
        run_id="first",
    )
    assert result["code"] == "INTERNAL_ERROR"
    assert app.conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?", (operation_id,)).fetchone()[0] == 1
    assert "120 g" in backend.notes

    monkeypatch.setattr(step8, "create_verification_cycle", original_create)
    recovered = recover_operation(app.conn, backend, operation_id=operation_id, requested_outcome="applied")
    assert any(str(action.get("step", "")).startswith("route_new_cycle:") for action in recovered["actions"])
    assert app.conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?", (operation_id,)).fetchone()[0] == 2
    assert app.conn.execute("SELECT phase FROM operations WHERE operation_id=?", (operation_id,)).fetchone()[0] == "await_verification"
