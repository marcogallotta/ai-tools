import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.admin import DishAdminApplication
from dish_tool.database import declare_operation_step
from dish_tool.step9 import recover_operation
from test_dish_tool_step5_commands import Backend, app


def test_planning_recovery_completes_missing_suffix(tmp_path):
    backend = Backend(title="Bare", notes="", section="planning")
    application = app(tmp_path, backend)
    started = application.execute("start", agent="claude", task_gid="t", kind="planning", change_level=None, change_reason=None)
    assert started["ok"]
    operation_id = started["submission_id"]
    notes = "### Planning brief\nDish candidate: Test\nPurpose: Compare\nRole: non-main — side\nPriors: None\nLocks: Keep crisp\nExemptions: None\nResearch emphasis: texture\nDestination section: Sichuan — 12345\n"
    declare_operation_step(application.conn, operation_id, "planning_write", {"title": "Bare", "notes": notes, "schema_version": "2"})
    declare_operation_step(application.conn, operation_id, "planning_handoff", {"section_gid": "rq"})
    declare_operation_step(application.conn, operation_id, "planning_terminal", {"status": "completed", "phase": "terminal", "terminal_outcome": "planning_handoff_confirmed"})
    backend.notes = notes

    result = recover_operation(application.conn, backend, operation_id=operation_id, requested_outcome="applied", reason="restart")
    assert backend.section == "rq"
    assert result["operation_status"] == "completed"
    assert application.conn.execute("SELECT COUNT(*) FROM operation_steps WHERE operation_id=? AND completed_at IS NULL", (operation_id,)).fetchone()[0] == 0


def test_current_operation_can_be_cancelled_only_from_safe_live_state(tmp_path):
    backend = Backend(title="Bare", notes="", section="rq")
    application = app(tmp_path, backend)
    started = application.execute("start", agent="claude", task_gid="t", kind="planning", change_level=None, change_reason=None)
    admin = DishAdminApplication(application.conn, backend=backend)
    result = admin.execute("discard", submission_id=started["submission_id"], reason="abandon clean operation")
    assert result["ok"]
    row = application.conn.execute("SELECT status, phase, terminal_outcome FROM operations WHERE operation_id=?", (started["submission_id"],)).fetchone()
    assert tuple(row) == ("cancelled", "terminal", "cancelled_by_marco")
