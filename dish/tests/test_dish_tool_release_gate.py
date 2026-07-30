import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.admin import DishAdminApplication
from dish_tool.cli import build_parser
from test_dish_tool_step6_prepare import Backend as PlanningBackend, PLANNING, TASK as RESEARCH_TASK, app as planning_app, write
from test_dish_tool_step7_verification import TASK, make_app


def test_planning_handoff_allows_next_research_operation(tmp_path):
    backend = PlanningBackend()
    app = planning_app(tmp_path, backend)
    planning = app.execute("start", agent="gpt", task_gid="t", kind="planning", run_id="plan-run")
    prepared = app.execute(
        "prepare", model="gpt-5.6-sol", agent="gpt", submission_id=planning["submission_id"],
        file_path=write(tmp_path, "planning.txt", PLANNING),
    )
    assert prepared["ok"]
    row = app.conn.execute("SELECT status, phase, terminal_outcome FROM operations WHERE operation_id=?", (planning["submission_id"],)).fetchone()
    assert tuple(row) == ("completed", "terminal", "planning_handoff_confirmed")
    research = app.execute("start", agent="gpt", task_gid="t", kind="initial", run_id="research-run")
    assert research["ok"]


def test_large_cycle_freezes_current_release_and_preserves_all_run_lineage(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    first = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="editor-run", independence_attestation="independent")
    assert first["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    protocol = app._load_release("verification").root / "dish-verification-protocol.md"
    protocol.write_text("# changed verification protocol\n")
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))
    result = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="material correction", file_path=str(candidate), run_id="editor-run",
    )
    assert result["ok"]
    cycles = app.conn.execute(
        "SELECT protocol_release FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number",
        (operation_id,),
    ).fetchall()
    assert len(cycles) == 2 and cycles[0][0] != cycles[1][0]
    barred = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="constructor-run", independence_attestation="independent")
    assert barred["code"] == "AGENT_MISMATCH"


def test_governed_lock_change_requires_human_authorization(tmp_path):
    app, _, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-run", independence_attestation="independent")
    assert review["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    candidate = tmp_path / "bad-large.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Remove crispness constraint"))
    result = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large",
        reason="change lock", file_path=str(candidate), run_id="review-run",
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "governed_change_unauthorized"


def test_evidence_hold_has_executable_resume_to_verification(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-run", independence_attestation="independent")
    assert review["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    held = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="evidence",
        reason="confirm source", resume_status="pending-verification", run_id="review-run",
    )
    assert held["ok"]
    admin = DishAdminApplication(
        app.conn, backend=backend,
        release_loader=lambda: app._load_release("verification"),
    )
    resumed = admin.execute(
        "supply-evidence", submission_id=operation_id,
        detail="Marco confirmed the source", resume_status="pending-verification",
    )
    assert resumed["ok"]
    assert "Status: pending-verification" in backend.notes
    op = app.conn.execute("SELECT phase FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert op[0] == "await_verification"
    assert app.conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?", (operation_id,)).fetchone()[0] == 2


def test_hold_resuming_research_clears_release_immediately(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-run", independence_attestation="independent")
    assert review["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    held = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="evidence",
        reason="new research needed", resume_status="pending-research", run_id="review-run",
    )
    assert held["ok"]
    assert "Status: pending-evidence" in backend.notes
    assert "Resume status: pending-research" in backend.notes
    assert "Verification protocol release: None" in backend.notes


def test_new_submit_cli_does_not_require_candidate_file():
    parsed = build_parser().parse_args(["submit", "operation-id"])
    assert parsed.submission_id == "operation-id"
    assert not hasattr(parsed, "file_path")
