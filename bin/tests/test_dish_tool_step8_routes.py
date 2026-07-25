import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.admin import DishAdminApplication
from test_dish_tool_step7_verification import TASK, make_app


def _review(app, agent, task="t", run="review"):
    result = app.execute("start", agent=agent, task_gid=task, kind="verification", run_id=run)
    assert result["ok"]
    return result


def test_small_correction_is_written_rechecked_and_signed_same_pass(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = _review(app, "codex")
    candidate = tmp_path / "small.txt"
    candidate.write_text(TASK.replace("1. Cook it.", "1. Cook it gently."))
    result = app.execute(
        "approve", agent="codex", submission_id=operation_id,
        correction="small", file_path=str(candidate),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True,
    )
    assert result["ok"]
    assert "small verification correction" in backend.notes
    assert "Status: ready" in backend.notes
    cycle = app.conn.execute("SELECT correction_class, outcome FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()
    assert tuple(cycle) == ("small", "approved")


def test_large_requires_fresh_verifier_and_two_pass_writes_task_hold(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app, "codex", run="first")
    candidate = tmp_path / "large.txt"; candidate.write_text(TASK.replace("100 g", "120 g"))
    first = app.execute("reject", agent="codex", submission_id=operation_id, route="large", reason="method needs replacement", file_path=str(candidate))
    assert first["ok"] and first["data"]["new_cycle_id"]
    barred = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="same-verifier")
    assert barred["code"] == "AGENT_MISMATCH"
    _review(app, "claude", run="second")
    candidate.write_text(TASK.replace("100 g", "130 g"))
    second = app.execute("reject", agent="claude", submission_id=operation_id, route="large", reason="premise still unresolved", file_path=str(candidate))
    assert second["ok"] and second["data"]["two_pass_hold"]
    assert "Status: pending-human-review" in backend.notes
    assert "Resume status: pending-verification" in backend.notes
    blocked = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="third")
    assert blocked["code"] == "WRONG_STATE"


def test_evidence_and_human_routes_require_protocol_reasons_and_resume(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app, "codex")
    bad = app.execute("reject", agent="codex", submission_id=operation_id, route="evidence", reason="missing source", resume_status=None)
    assert bad["code"] == "INVALID_ARGUMENT"
    good = app.execute("reject", agent="codex", submission_id=operation_id, route="evidence", reason="Marco must confirm the factual input", resume_status="pending-verification")
    assert good["ok"] and "Status: pending-evidence" in backend.notes


def test_marco_reopen_requires_substantive_change_and_retains_cycles(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"; candidate.write_text(TASK)
    _review(app, "codex", run="one")
    app.execute("reject", agent="codex", submission_id=operation_id, route="large", reason="first", file_path=str(candidate))
    _review(app, "claude", run="two")
    app.execute("reject", agent="claude", submission_id=operation_id, route="large", reason="second", file_path=str(candidate))
    admin = DishAdminApplication(app.conn, backend=backend)
    bad = admin.execute("reopen", submission_id=operation_id, category="hash", before="a", after="b", editor="Marco", date="2026-07-25")
    assert bad["code"] == "INVALID_ARGUMENT"
    result = admin.execute("reopen", submission_id=operation_id, category="premise", before="old premise", after="new premise", editor="Marco", date="2026-07-25")
    assert result["ok"]
    assert "Status: pending-verification" in backend.notes
    assert "before: old premise; after: new premise" in backend.notes
    assert app.conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()[0] == 3
