import json
from pathlib import Path

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.database import confirm_task_content, create_operation, declare_operation_step
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from dish_tool.recovery import begin_movement_attempt, begin_operation_write_attempt


class Backend:
    def __init__(self, task_gid, title, notes, section="research"):
        self.task_gid = task_gid
        self.title = title
        self.notes = notes
        self.section = section

    def read_task(self, task_gid):
        return {
            "gid": task_gid,
            "name": self.title,
            "notes": self.notes,
            "completed": False,
            "modified_at": "fixture",
            "memberships": [{"project": {"gid": "1215089183018968"}, "section": {"gid": self.section}}],
        }

    def list_sections(self, project_gid):
        return [
            {"gid": self.section, "name": "Research Queue"},
            {"gid": "verification-queue", "name": "Verification Queue"},
            {"gid": "sourcing", "name": "Sourcing"},
            {"gid": "reference", "name": "Reference"},
        ]


def _operation(tmp_path: Path):
    conn = initialize_database(tmp_path / "db.sqlite")
    task_gid = "task"
    baseline = confirm_task_content(conn, task_gid=task_gid, title="Dish", notes="baseline", schema_version="2")
    op = create_operation(
        conn,
        task_gid=task_gid,
        operation_kind="initial",
        expected_identity=baseline.digest,
        schema_version="2",
        actors=OperationActors(editor_agent="gpt", run_id="run-a"),
    )
    return conn, op, baseline


def test_retry_step_intent_must_match_exactly(tmp_path):
    conn, op, _ = _operation(tmp_path)
    declare_operation_step(conn, op["operation_id"], "candidate_write", {"title": "A", "notes": "one"})
    declare_operation_step(conn, op["operation_id"], "candidate_write", {"title": "A", "notes": "one"})
    with pytest.raises(DishRuleError) as exc:
        declare_operation_step(conn, op["operation_id"], "candidate_write", {"title": "B", "notes": "two"})
    assert exc.value.rule == "operation_step_intent_mismatch"


def test_new_attempt_is_blocked_while_older_attempt_is_unresolved(tmp_path):
    conn, op, baseline = _operation(tmp_path)
    begin_operation_write_attempt(
        conn,
        operation_id=op["operation_id"],
        expected_identity=baseline.digest,
        intended_identity="new",
        intended_title="Dish",
        intended_notes="new",
        schema_version="2",
    )
    with pytest.raises(DishRuleError) as exc:
        begin_operation_write_attempt(
            conn,
            operation_id=op["operation_id"],
            expected_identity=baseline.digest,
            intended_identity="newer",
            intended_title="Dish",
            intended_notes="newer",
            schema_version="2",
        )
    assert exc.value.rule == "unresolved_write_attempt"

    begin_movement_attempt(conn, operation_id=op["operation_id"], expected_section_gid="research", intended_section_gid="verification")
    with pytest.raises(DishRuleError) as exc:
        begin_movement_attempt(conn, operation_id=op["operation_id"], expected_section_gid="research", intended_section_gid="verification")
    assert exc.value.rule == "unresolved_movement_attempt"


def test_cancellation_rejects_confirmed_intermediate_mutation(tmp_path):
    conn, op, baseline = _operation(tmp_path)
    confirm_task_content(conn, task_gid=op["task_gid"], title="Dish", notes="baseline", schema_version="2", operation_id=op["operation_id"], boundary="content_write")
    conn.execute(
        """INSERT INTO write_attempts(
            attempt_id, operation_id, expected_identity, intended_identity, outcome,
            started_at, finished_at, purpose, intended_title, intended_notes,
            schema_version, confirmed_content_version_id
        ) VALUES('attempt',?,?,?,?,?,?,?,?,?,?,?)""",
        (
            op["operation_id"], baseline.digest, baseline.digest, "confirmed",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "content_write",
            "Dish", "baseline", "2",
            conn.execute("SELECT content_version_id FROM content_versions WHERE task_gid=? ORDER BY rowid DESC LIMIT 1", (op["task_gid"],)).fetchone()[0],
        ),
    )
    app = DishAdminApplication(conn, backend=Backend(op["task_gid"], "Dish", "baseline"))
    result = app.execute("discard", submission_id=op["operation_id"], reason="stop")
    assert result["ok"] is False
    assert result["errors"][-1]["rule"] == "operation_cancel_applied_effects"
    assert conn.execute("SELECT status FROM operations WHERE operation_id=?", (op["operation_id"],)).fetchone()[0] == "open"
