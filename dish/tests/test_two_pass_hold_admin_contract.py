from dish_tool.admin import DishAdminApplication
from tests.support.verification import TASK, make_app, review_and_inspect


def test_verification_hold_advertises_resolved_and_releases_unchanged_candidate(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"
    for index, (agent, run_id, amount) in enumerate((
        ("codex", "first", "120 g"),
        ("gpt", "second", "130 g"),
        ("claude", "third", "140 g"),
    ), start=1):
        review_and_inspect(app, agent=agent, run_id=run_id)
        candidate.write_text(TASK.replace("100 g", amount))
        result = app.execute(
            "reject", agent=agent, model="gpt-5.6-sol", submission_id=operation_id,
            route="large", reason=f"failure {index}", file_path=str(candidate), run_id=run_id,
        )
        assert result["ok"]
        if index < 3:
            assert result["data"]["verification_hold"] is False
            assert result["data"]["new_cycle_id"]
        else:
            assert result["data"]["verification_hold"] is True

    held = backend.notes
    assert "140 g" in held
    assert app.conn.execute(
        "SELECT outcome FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()[0] == "verification-hold"
    assert result["allowed_actions"] == []
    assert result["data"]["required_admin_action"] == "resolved"
    assert result["data"]["admin_command"] == f"dish-admin resolved {operation_id}"

    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release(None))
    released = admin.execute("resolved", submission_id=operation_id)
    assert released["ok"]
    assert released["data"]["approved"] is False
    assert released["data"]["signed_off"] is False
    assert released["data"]["new_cycle_id"]
    assert "140 g" in backend.notes
    assert "Status: pending-verification" in backend.notes
    assert "Verified by: None" in backend.notes
    assert released["allowed_actions"] == ["verify"]


def test_review_approve_wrapper_keeps_public_command_identity_for_verification_hold(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"
    for index, (agent, run_id, amount) in enumerate((
        ("codex", "first-wrapper", "120 g"),
        ("gpt", "second-wrapper", "130 g"),
        ("claude", "third-wrapper", "140 g"),
    ), start=1):
        review_and_inspect(app, agent=agent, run_id=run_id)
        candidate.write_text(TASK.replace("100 g", amount))
        result = app.execute(
            "reject", agent=agent, model="gpt-5.6-sol", submission_id=operation_id,
            route="large", reason=f"failure {index}", file_path=str(candidate), run_id=run_id,
        )
        assert result["ok"]

    cycle_id = app.conn.execute(
        "SELECT cycle_id FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()[0]
    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=lambda: app._load_release(None)
    )
    released = admin.execute("review-approve", proposal_id=cycle_id)

    assert released["ok"]
    assert released["command"] == "review-approve"
    assert released["data"]["new_cycle_id"]
    assert "Status: pending-verification" in backend.notes
