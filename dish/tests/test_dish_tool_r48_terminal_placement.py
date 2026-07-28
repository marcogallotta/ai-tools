from dish_tool.admin import DishAdminApplication
from dish_tool.recovery import begin_movement_attempt, finish_movement_attempt
from tests.test_dish_tool_step7_verification import make_app


def _approve(app, operation_id: str) -> None:
    review = app.execute(
        "start", agent="codex", task_gid="t", kind="verification", run_id="review",
        independence_attestation="independent",
    )
    assert review["ok"]
    approved = app.execute(
        "approve",
        model="gpt-5.6-sol",
        agent="codex",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="review",
        independence_attestation="independent",
    )
    assert approved["ok"]


def _approve_and_submit(app, operation_id: str) -> None:
    _approve(app, operation_id)
    submitted = app.execute("submit", submission_id=operation_id)
    assert submitted["ok"]


def _placement(app, operation_id: str) -> dict[str, object]:
    inspected = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["ok"]
    return inspected["data"]["authoritative_view"]


def test_approved_confirmed_submit_requires_destination_placement(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)

    _approve_and_submit(app, operation_id)

    view = _placement(app, operation_id)
    assert backend.section == "12345"
    assert view["status"] == "completed"
    assert view["phase"] == "terminal"
    assert view["required_section_gid"] == "12345"
    assert view["required_section_name"] == "Sichuan"
    assert view["placement_matches"] is True
    assert view["recovery_required"] is False


def test_completed_task_already_present_in_destination_matches(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve(app, operation_id)
    backend.section = "12345"

    submitted = app.execute("submit", submission_id=operation_id)

    assert submitted["ok"]
    assert submitted["data"]["handoff"] == "already_at_destination"
    view = _placement(app, operation_id)
    assert view["required_section_gid"] == "12345"
    assert view["required_section_name"] == "Sichuan"
    assert view["placement_matches"] is True


def test_completed_task_back_in_verification_queue_reports_destination_drift(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    backend.section = "vq"

    view = _placement(app, operation_id)

    assert view["required_section_gid"] == "12345"
    assert view["required_section_name"] == "Sichuan"
    assert view["live_section_gid"] == "vq"
    assert view["placement_matches"] is False
    assert view["recovery_required"] is False


def test_uncertain_destination_movement_keeps_pre_recovery_requirement(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve(app, operation_id)
    attempt_id = begin_movement_attempt(
        app.conn,
        operation_id=operation_id,
        expected_section_gid="vq",
        intended_section_gid="12345",
        purpose="destination_submission",
    )
    finish_movement_attempt(app.conn, attempt_id=attempt_id, outcome="uncertain")

    view = _placement(app, operation_id)

    assert backend.section == "vq"
    assert view["required_section_gid"] is None
    assert view["required_section_name"] is None
    assert view["placement_matches"] is True
    assert view["recovery_required"] is True
    assert view["unresolved_attempts"] == [f"movement:{attempt_id}"]


def test_recovered_destination_movement_becomes_required_placement(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve(app, operation_id)
    attempt_id = begin_movement_attempt(
        app.conn,
        operation_id=operation_id,
        expected_section_gid="vq",
        intended_section_gid="12345",
        purpose="destination_submission",
    )
    finish_movement_attempt(app.conn, attempt_id=attempt_id, outcome="uncertain")
    backend.section = "12345"
    admin = DishAdminApplication(app.conn, backend=backend)

    recovered = admin.execute(
        "recover",
        submission_id=operation_id,
        outcome="applied",
        reason="confirmed destination move after restart",
    )

    assert recovered["ok"]
    view = _placement(app, operation_id)
    assert view["required_section_gid"] == "12345"
    assert view["required_section_name"] == "Sichuan"
    assert view["placement_matches"] is True
    assert view["recovery_required"] is False
