import pytest

from dish_tool.database import confirm_task_content, create_operation, initialize_database
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.task_store import (
    assert_live_matches_confirmed,
    move_exact,
    write_exact_content,
)

PROJECT = "project"


def raw_task(title="Old", notes="Body", section="research", *, completed=False):
    return {
        "gid": "task",
        "name": title,
        "notes": notes,
        "completed": completed,
        "modified_at": "2026-07-25T12:00:00Z",
        "memberships": [{"project": {"gid": PROJECT}, "section": {"gid": section}}],
    }


class Backend:
    def __init__(self):
        self.task = raw_task()
        self.writes = 0
        self.moves = 0
        self.write_mode = "apply"
        self.move_mode = "apply"

    def read_task(self, task_gid):
        return {**self.task, "memberships": [dict(self.task["memberships"][0])]}

    def update_task_content(self, *, task_gid, title, notes):
        self.writes += 1
        if self.write_mode == "reject":
            raise BackendFailure("BACKEND_REJECTED", "rejected", retryable=True)
        if self.write_mode == "timeout_apply":
            self.task.update(name=title, notes=notes)
            raise BackendFailure("BACKEND_UNCERTAIN", "timeout", retryable=False)
        if self.write_mode == "mismatch":
            self.task.update(name=title, notes=notes + " changed")
            return
        self.task.update(name=title, notes=notes)

    def move_task_to_section(self, *, task_gid, section_gid):
        self.moves += 1
        if self.move_mode == "reject":
            raise BackendFailure("BACKEND_REJECTED", "rejected", retryable=True)
        self.task["memberships"][0]["section"] = {"gid": section_gid}


def setup(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    backend = Backend()
    identity = confirm_task_content(
        conn, task_gid="task", title="Old", notes="Body", schema_version="2", boundary="baseline"
    ).digest
    operation = create_operation(
        conn, task_gid="task", operation_kind="change", expected_identity=identity, schema_version="2"
    )
    return conn, backend, identity, operation["operation_id"]


def test_stale_live_content_blocks_before_mutation(tmp_path):
    conn, backend, identity, operation_id = setup(tmp_path)
    backend.task["notes"] = "manual edit"
    with pytest.raises(DishRuleError, match="outside the guarded operation"):
        write_exact_content(
            conn, backend, operation_id=operation_id, task_gid="task", project_gid=PROJECT,
            expected_identity=identity, expected_section_gid="research", title="New", notes="New body",
            schema_version="2",
        )
    assert backend.writes == 0


def test_exact_write_succeeds_and_rereads(tmp_path):
    conn, backend, identity, operation_id = setup(tmp_path)
    result = write_exact_content(
        conn, backend, operation_id=operation_id, task_gid="task", project_gid=PROJECT,
        expected_identity=identity, expected_section_gid="research", title="New", notes="New body",
        schema_version="2",
    )
    assert (result.title, result.notes) == ("New", "New body")
    state = conn.execute("SELECT * FROM task_content_state WHERE task_gid='task'").fetchone()
    assert state["last_confirmed_title"] == "New"
    operation = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert operation["content_write_completed_at"]


def test_timeout_that_applied_is_confirmed(tmp_path):
    conn, backend, identity, operation_id = setup(tmp_path)
    backend.write_mode = "timeout_apply"
    result = write_exact_content(
        conn, backend, operation_id=operation_id, task_gid="task", project_gid=PROJECT,
        expected_identity=identity, expected_section_gid="research", title="New", notes="New body",
        schema_version="2",
    )
    assert result.title == "New"
    attempt = conn.execute("SELECT outcome FROM write_attempts").fetchone()
    assert attempt["outcome"] == "confirmed"


def test_clear_non_application_is_retryable(tmp_path):
    conn, backend, identity, operation_id = setup(tmp_path)
    backend.write_mode = "reject"
    with pytest.raises(BackendFailure) as caught:
        write_exact_content(
            conn, backend, operation_id=operation_id, task_gid="task", project_gid=PROJECT,
            expected_identity=identity, expected_section_gid="research", title="New", notes="New body",
            schema_version="2",
        )
    assert caught.value.code == "BACKEND_REJECTED"
    assert caught.value.retryable
    assert conn.execute("SELECT outcome FROM write_attempts").fetchone()["outcome"] == "not_applied"


def test_post_write_mismatch_is_uncertain(tmp_path):
    conn, backend, identity, operation_id = setup(tmp_path)
    backend.write_mode = "mismatch"
    with pytest.raises(BackendFailure) as caught:
        write_exact_content(
            conn, backend, operation_id=operation_id, task_gid="task", project_gid=PROJECT,
            expected_identity=identity, expected_section_gid="research", title="New", notes="New body",
            schema_version="2",
        )
    assert caught.value.code == "BACKEND_UNCERTAIN"
    assert conn.execute("SELECT outcome FROM write_attempts").fetchone()["outcome"] == "uncertain"


def test_movement_never_rewrites_content(tmp_path):
    conn, backend, identity, operation_id = setup(tmp_path)
    result = move_exact(
        conn, backend, operation_id=operation_id, task_gid="task", project_gid=PROJECT,
        expected_identity=identity, expected_section_gid="research", intended_section_gid="verification",
    )
    assert result.section_gid == "verification"
    assert (result.title, result.notes) == ("Old", "Body")
    assert backend.writes == 0


def test_out_of_band_edit_detected_between_operations_and_metadata_ignored(tmp_path):
    conn, backend, _, _ = setup(tmp_path)
    backend.task["notes"] = "manual"
    with pytest.raises(DishRuleError) as caught:
        assert_live_matches_confirmed(
            conn, backend, task_gid="task", project_gid=PROJECT, expected_section_gid="research"
        )
    assert caught.value.rule == "live_task_drift"

    backend.task["notes"] = "Body"
    backend.task["completed"] = True
    assert_live_matches_confirmed(
        conn, backend, task_gid="task", project_gid=PROJECT, expected_section_gid="research"
    )


def test_signed_task_rename_detected(tmp_path):
    conn, backend, _, _ = setup(tmp_path)
    backend.task["name"] = "Renamed"
    with pytest.raises(DishRuleError) as caught:
        assert_live_matches_confirmed(
            conn, backend, task_gid="task", project_gid=PROJECT, expected_section_gid="research"
        )
    assert caught.value.rule == "live_task_drift"
