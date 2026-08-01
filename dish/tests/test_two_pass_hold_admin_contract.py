from dish_tool.admin import DishAdminApplication
from tests.support.verification import TASK, make_app, review_and_inspect


def test_two_pass_hold_advertises_reopen_not_human_decision(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))
    review_and_inspect(app, agent="codex", run_id="first")
    first = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="first failure", file_path=str(candidate), run_id="first",
    )
    assert first["ok"]

    review_and_inspect(app, agent="gpt", run_id="second")
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
