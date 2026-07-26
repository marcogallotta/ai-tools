import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.task_document import parse_task_document, validate_task_document
from test_dish_tool_step7_verification import TASK, make_app


def test_approval_phase_response_and_inspect_agree(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review")
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
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review")
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
