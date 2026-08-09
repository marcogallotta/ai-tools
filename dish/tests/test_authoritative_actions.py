from pathlib import Path


from dish_tool.task_document import parse_task_document, validate_task_document
from tests.support.verification import TASK, make_app, review_and_inspect


def test_approval_phase_response_and_inspect_agree(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review", independence_attestation="independent")
    inspected_review = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected_review["ok"]
    approved = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True,
        provenance_complete=True, run_id="review",
    )
    assert approved["allowed_actions"] == ["submit"]
    row = app.conn.execute("SELECT status, phase FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert tuple(row) == ("open", "await_submission")
    inspected = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["allowed_actions"] == ["submit"]
    assert inspected["data"]["authoritative_view"]["live_status"] == "ready"


def test_verification_requires_current_verification_queue_placement(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    backend.section = "rq"
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review", independence_attestation="independent")
    assert result["code"] == "WRONG_STATE"
    assert result["errors"][0]["rule"] == "verification_placement_required"
    inspected = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["allowed_actions"] == []


def test_hold_state_matrix_rejects_release_and_signoff_when_resuming_research():
    text = TASK.replace("Status: pending-research", "Status: pending-human-review")
    text = text.replace("Status detail: Continue research", "Status detail: Marco must decide")
    text = text.replace("Resume status: None", "Resume status: pending-research")
    text = text.replace("Verification protocol release: None", "Verification protocol release: sha256:stale")
    text = text.replace("Verified by: None", "Verified by: ChatGPT — Codex, 2026-07-25")
    doc = parse_task_document(text)
    rules = {finding.rule for finding in validate_task_document(doc).findings}
    assert "state.illegal-combination" in rules


def test_submit_terminal_response_and_inspect_expose_no_stale_actions(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="terminal-review",
        independence_attestation="independent",
    )
    inspected_review = app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )
    assert inspected_review["allowed_actions"] == ["approve", "reject"]

    approved = app.execute(
        "approve",
        model="gpt-5.6-sol",
        agent="codex",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="terminal-review",
    )
    assert approved["allowed_actions"] == ["submit"]

    submitted = app.execute("submit", submission_id=operation_id)
    assert submitted["ok"], submitted
    assert submitted["state"] == "completed"
    assert submitted["allowed_actions"] == []
    assert submitted["data"]["handoff"] == "moved_to_destination"

    terminal = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert terminal["ok"], terminal
    assert terminal["state"] == "completed"
    assert terminal["allowed_actions"] == []
    assert terminal["data"]["legal_next_actions"] == []
    assert "required_start_kind" not in terminal["data"]
    assert "required_admin_action" not in terminal["data"]


def test_large_rejection_response_and_inspect_expose_same_fresh_verification_start(
    tmp_path,
):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="large-review")
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))

    rejected = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="material correction required",
        file_path=str(candidate),
        run_id="large-review",
    )
    assert rejected["ok"], rejected
    assert rejected["data"]["verification_hold"] is False
    assert rejected["allowed_actions"] == ["start"]
    assert rejected["data"]["required_start_kind"] == "verification"

    refreshed = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert refreshed["ok"], refreshed
    assert refreshed["allowed_actions"] == ["start"]
    assert refreshed["data"]["legal_next_actions"] == ["start"]
    assert refreshed["data"]["required_start_kind"] == "verification"
