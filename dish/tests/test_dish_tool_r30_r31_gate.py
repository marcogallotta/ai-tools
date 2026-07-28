import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.admin import DishAdminApplication
from dish_tool import step7, step8
from test_dish_tool_step7_verification import TASK, make_app


def _review(app, agent, run):
    result = app.execute("start", agent=agent, task_gid="t", kind="verification", run_id=run, independence_attestation="independent")
    assert result["ok"]
    inspected = app.execute("inspect", agent=agent, submission_id=result["submission_id"])
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result


def test_verification_read_local_facts_are_atomic(monkeypatch, tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)

    def fail_actor(*args, **kwargs):
        raise RuntimeError("injected actor persistence failure")

    monkeypatch.setattr(step7, "record_actor_fact", fail_actor)
    failed = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-run", independence_attestation="independent")
    assert not failed["ok"]

    cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()
    assert cycle["reviewed_content_version_id"] is None
    assert cycle["reviewed_identity"] is None
    assert cycle["verifier_agent"] is None
    assert cycle["run_id"] is None
    op = app.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert op["verifier_agent"] is None
    assert app.conn.execute(
        "SELECT COUNT(*) FROM operation_actor_facts WHERE operation_id=? AND role='verifier'",
        (operation_id,),
    ).fetchone()[0] == 0
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='verification.review_started'",
        (operation_id,),
    ).fetchone()[0] == 0

    monkeypatch.undo()
    retry = _review(app, "codex", "review-run")
    assert retry["allowed_actions"] == ["inspect"]


def test_large_route_actor_is_recoverable_before_cycle_is_usable(monkeypatch, tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app, "codex", "large-editor")
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))

    original = step8.record_actor_fact
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        if kwargs.get("role") == "material_editor" and calls["count"] == 0:
            calls["count"] += 1
            raise RuntimeError("injected route actor failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(step8, "record_actor_fact", fail_once)
    failed = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="material correction", file_path=str(candidate), run_id="large-editor",
        independence_attestation="independent",
    )
    assert not failed["ok"]
    assert app.conn.execute(
        "SELECT COUNT(*) FROM operation_steps WHERE operation_id=? AND step_name LIKE 'route_actor:%' AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()[0] == 1
    assert app.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()[0] == 1  # only the old cycle; no corrected cycle is usable yet

    monkeypatch.undo()
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release(None))
    recovered = admin.execute("recover", submission_id=operation_id, outcome="applied", reason="resume route suffix")
    assert recovered["ok"]
    fact = app.conn.execute(
        "SELECT * FROM operation_actor_facts WHERE operation_id=? AND role='material_editor' AND run_id='large-editor'",
        (operation_id,),
    ).fetchone()
    assert fact is not None
    assert fact["candidate_identity"]

    barred = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="large-editor", independence_attestation="independent")
    assert barred["code"] == "AGENT_MISMATCH"


def test_two_pass_hold_advertises_reopen_not_human_decision(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))
    _review(app, "codex", "first")
    first = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="first failure", file_path=str(candidate), run_id="first",
        independence_attestation="independent",
    )
    assert first["ok"]

    _review(app, "gpt", "second")
    candidate.write_text(TASK.replace("100 g", "130 g"))
    second = app.execute(
        "reject", agent="gpt", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="second failure", file_path=str(candidate), run_id="second",
        independence_attestation="independent",
    )
    assert second["ok"] and second["data"]["two_pass_hold"]
    assert second["allowed_actions"] == []
    assert second["data"]["required_admin_action"] == "reopen"

    inspected = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["ok"]
    assert inspected["allowed_actions"] == []
    assert inspected["data"]["required_admin_action"] == "reopen"

    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release(None))
    wrong = admin.execute(
        "record-human-decision", submission_id=operation_id,
        detail="continue", resume_status="pending-verification",
    )
    assert wrong["code"] == "WRONG_STATE"
