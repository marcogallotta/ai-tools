from dish_tool.admin import DishAdminApplication
from tests.support.verification import TASK, make_app


def _review(app, agent, run):
    result = app.execute("start", agent=agent, task_gid="t", kind="verification", run_id=run, independence_attestation="independent")
    assert result["ok"]
    inspected = app.execute("inspect", agent=agent, submission_id=result["submission_id"])
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result


def test_two_pass_hold_advertises_reopen_not_human_decision(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))
    _review(app, "codex", "first")
    first = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="first failure", file_path=str(candidate), run_id="first",
    )
    assert first["ok"]

    _review(app, "gpt", "second")
    candidate.write_text(TASK.replace("100 g", "130 g"))
    second = app.execute(
        "reject", agent="gpt", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="second failure", file_path=str(candidate), run_id="second",
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
