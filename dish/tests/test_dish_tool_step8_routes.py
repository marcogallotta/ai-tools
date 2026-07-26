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
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="small", file_path=str(candidate),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review",
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
    first = app.execute("reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large", reason="method needs replacement", file_path=str(candidate), run_id="first")
    assert first["ok"] and first["data"]["new_cycle_id"]
    barred = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="first")
    assert barred["code"] == "AGENT_MISMATCH"
    _review(app, "gpt", run="second")
    candidate.write_text(TASK.replace("100 g", "130 g"))
    second = app.execute("reject", agent="gpt", model="gpt-5.6-sol", submission_id=operation_id, route="large", reason="premise still unresolved", file_path=str(candidate), run_id="second")
    assert second["ok"] and second["data"]["two_pass_hold"]
    assert "Status: pending-human-review" in backend.notes
    assert "Resume status: pending-verification" in backend.notes
    blocked = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="third")
    assert blocked["code"] == "WRONG_STATE"


def test_evidence_and_human_routes_require_protocol_reasons_and_resume(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app, "codex")
    bad = app.execute("reject", agent="codex", submission_id=operation_id, route="evidence", reason="missing source", resume_status=None, run_id="review")
    assert bad["code"] == "INVALID_ARGUMENT"
    good = app.execute("reject", agent="codex", submission_id=operation_id, route="evidence", reason="Marco must confirm the factual input", resume_status="pending-verification", run_id="review")
    assert good["ok"] and "Status: pending-evidence" in backend.notes


def test_marco_reopen_requires_substantive_change_and_retains_cycles(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"; candidate.write_text(TASK)
    _review(app, "codex", run="one")
    app.execute("reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large", reason="first", file_path=str(candidate), run_id="one")
    _review(app, "gpt", run="two")
    app.execute("reject", agent="gpt", model="gpt-5.6-sol", submission_id=operation_id, route="large", reason="second", file_path=str(candidate), run_id="two")
    admin = DishAdminApplication(app.conn, backend=backend)
    bad = admin.execute("reopen", submission_id=operation_id, category="hash", before="a", after="b", editor="codex", model="gpt-5.6-sol", run_id="reopen-run", file_path=str(candidate), date="2026-07-25")
    assert bad["code"] == "INVALID_ARGUMENT"
    corrected = tmp_path / "reopened.txt"
    corrected.write_text(f"{backend.title}\n{backend.notes}".replace("Compare hydration routes.", "Compare hydration routes with a rested-starch reset."))
    result = admin.execute(
        "reopen", submission_id=operation_id, category="premise",
        before="Compare hydration routes.",
        after="Compare hydration routes with a rested-starch reset.",
        editor="codex", model="gpt-5.6-sol", run_id="reopen-run", file_path=str(corrected), date="2026-07-25",
    )
    assert result["ok"]
    assert "Status: pending-verification" in backend.notes
    assert "before: Compare hydration routes.; after: Compare hydration routes with a rested-starch reset." in backend.notes
    assert app.conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()[0] == 3


def test_small_correction_cannot_replace_unreviewed_live_content(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = _review(app, "codex", run="small-proof")
    backend.title = backend.title.replace("Test dish", "Externally changed dish")
    candidate = tmp_path / "small-unreviewed.txt"
    candidate.write_text(TASK.replace("1. Cook it.", "1. Cook it gently."))
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="small",
        file_path=str(candidate), reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="small-proof",
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "stale_verifier_review"


def test_reject_requires_exact_verifier_run_proof(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app, "codex", run="reject-proof")
    result = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="evidence",
        reason="missing evidence", resume_status="pending-verification", run_id="wrong-run",
    )
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "verifier_proof_mismatch"
