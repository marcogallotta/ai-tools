from pathlib import Path
import sqlite3

import pytest

from dish_tool import step6, step8
from dish_tool.admin import DishAdminApplication
from dish_tool.errors import DishRuleError
from dish_tool.step9 import recover_operation
from test_dish_tool_step7_verification import Backend, TASK, make_app


def _review(app, run: str, agent: str = "codex"):
    result = app.execute("start", agent=agent, task_gid="t", kind="verification", run_id=run, independence_attestation="independent")
    assert result["ok"]
    inspected = app.execute("inspect", agent=agent, submission_id=result["submission_id"])
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result


def _approve_and_submit(app, operation_id: str, run: str = "review"):
    review = _review(app, run)
    approved = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id=run,
    )
    assert approved["ok"]
    submitted = app.execute("submit", submission_id=operation_id)
    assert submitted["ok"]


def test_reopen_recovery_records_editor_before_cycle_is_usable(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK)
    _review(app, "one")
    assert app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="first", file_path=str(candidate), run_id="one",
    )["ok"]
    _review(app, "two", agent="gpt")
    assert app.execute(
        "reject", agent="gpt", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="second", file_path=str(candidate), run_id="two",
    )["ok"]

    corrected = tmp_path / "corrected.txt"
    corrected.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Compare hydration routes.",
            "Compare hydration routes after a substantive premise reset.",
        )
    )
    original = step8.record_actor_fact

    def crash_before_actor(*args, **kwargs):
        raise RuntimeError("crash before editor lineage")

    monkeypatch.setattr(step8, "record_actor_fact", crash_before_actor)
    admin = DishAdminApplication(app.conn, backend=backend)
    failed = admin.execute(
        "reopen", model="gpt-5.6-sol", submission_id=operation_id, category="premise",
        before="Compare hydration routes.",
        after="Compare hydration routes after a substantive premise reset.",
        editor="codex", run_id="reopen-run", file_path=str(corrected), date="2026-07-25",
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["data"]["write_committed"] is True
    assert failed["data"]["failed_step"] == "reopen_actor"
    assert failed["data"]["required_admin_action"] == "recover"
    assert failed["data"]["safe_to_retry"] is False
    assert app.conn.execute(
        "SELECT COUNT(*) FROM operation_actor_facts WHERE task_gid='t' AND run_id='reopen-run'"
    ).fetchone()[0] == 0

    monkeypatch.setattr(step8, "record_actor_fact", original)
    recovered = recover_operation(app.conn, backend, operation_id=operation_id, requested_outcome="applied")
    assert any(action.get("step") == "reopen_actor" for action in recovered["actions"])
    barred = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="reopen-run", independence_attestation="independent")
    assert barred["code"] == "AGENT_MISMATCH"


def test_material_hold_resolution_recovery_records_editor_before_cycle(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app, "hold-review")
    held = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="evidence",
        reason="confirm source", resume_status="pending-verification", run_id="hold-review",
    )
    assert held["ok"]
    corrected = tmp_path / "hold-corrected.txt"
    corrected.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Crisp and aromatic.", "Crisp and aromatic with the confirmed source."
        )
    )
    original = step8.record_actor_fact

    def crash_before_actor(*args, **kwargs):
        raise RuntimeError("crash before hold editor lineage")

    monkeypatch.setattr(step8, "record_actor_fact", crash_before_actor)
    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=lambda: app._load_release("verification")
    )
    failed = admin.execute(
        "supply-evidence", submission_id=operation_id, detail="Marco confirmed source",
        resume_status="pending-verification", file_path=str(corrected),
        editor="codex", model="gpt-5.6-sol", run_id="hold-editor",
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["data"]["write_committed"] is True
    assert failed["data"]["failed_step"] == "hold_resolution_actor"
    assert failed["data"]["required_admin_action"] == "recover"
    assert failed["data"]["safe_to_retry"] is False
    monkeypatch.setattr(step8, "record_actor_fact", original)
    recovered = recover_operation(app.conn, backend, operation_id=operation_id, requested_outcome="applied")
    assert any(action.get("step") == "hold_resolution_actor" for action in recovered["actions"])
    barred = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="hold-editor", independence_attestation="independent")
    assert barred["code"] == "AGENT_MISMATCH"


def test_non_material_terminal_phase_recovers_after_confirmed_write(tmp_path, monkeypatch):
    app, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(app, initial_operation)
    started = app.execute(
        "start", agent="codex", task_gid="t", kind="change",
        change_level="small", change_reason="wording", run_id="later-editor",
    )
    candidate = tmp_path / "non-material.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "1. Cook it.", "1. Cook it gently."
        )
    )
    original = step6.transition_operation

    def crash_before_terminal(*args, **kwargs):
        raise RuntimeError("crash before non-material terminal")

    monkeypatch.setattr(step6, "transition_operation", crash_before_terminal)
    failed = app.execute(
        "prepare", model="gpt-5.6-sol", agent="codex", submission_id=started["submission_id"],
        file_path=str(candidate), material_classification="non-material",
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["data"]["write_committed"] is True
    assert failed["data"]["failed_step"] == "non_material_terminal"
    assert failed["data"]["required_admin_action"] == "recover"
    assert failed["data"]["safe_to_retry"] is False
    monkeypatch.setattr(step6, "transition_operation", original)
    recovered = recover_operation(
        app.conn, backend, operation_id=started["submission_id"], requested_outcome="applied"
    )
    assert any(action.get("step") == "non_material_terminal" for action in recovered["actions"])
    row = app.conn.execute(
        "SELECT status, phase FROM operations WHERE operation_id=?", (started["submission_id"],)
    ).fetchone()
    assert tuple(row) == ("completed", "terminal")


def test_completed_research_handoff_step_cannot_be_regressed(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        app.conn.execute(
            "UPDATE operation_steps SET completed_at=NULL WHERE operation_id=? AND step_name='verification_phase'",
            (operation_id,),
        )


def test_current_dispatch_contract_is_explicit_and_complete():
    from dish_tool.admin import CURRENT_ADMIN_COMMAND_HANDLERS
    from dish_tool.commands import CURRENT_COMMAND_HANDLERS

    assert set(CURRENT_COMMAND_HANDLERS) == {
        "sections", "create", "read", "inspect", "start",
        "prepare", "approve", "reject", "submit",
    }
    assert set(CURRENT_ADMIN_COMMAND_HANDLERS) == {
        "migrate", "reopen-planning", "reopen", "recover", "repair-destination", "supply-evidence",
        "record-human-decision", "authorize-governed-change", "discard",
    }
    assert all(callable(handler) for handler in CURRENT_COMMAND_HANDLERS.values())
    assert all(callable(handler) for handler in CURRENT_ADMIN_COMMAND_HANDLERS.values())
