"""Tests for batch-plan loading, validation, and normalization (pure logic, no API calls)."""
import json

import pytest


def test_load_batch_plan_missing_file(cli):
    with pytest.raises(SystemExit, match="batch plan not found"):
        cli._load_batch_plan("/nonexistent/plan.json")


def test_load_batch_plan_invalid_json(cli, tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("not json")
    with pytest.raises(SystemExit, match="invalid batch JSON"):
        cli._load_batch_plan(str(path))


def test_load_batch_plan_not_an_object(cli, tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("[]")
    with pytest.raises(SystemExit, match="must be a JSON object"):
        cli._load_batch_plan(str(path))


def test_load_batch_plan_empty_operations(cli, tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"operations": []}))
    with pytest.raises(SystemExit, match="non-empty operations array"):
        cli._load_batch_plan(str(path))


def test_load_batch_plan_returns_ops(cli, tmp_path):
    ops = [{"action": "rename", "task": "1", "name": "x", "reason": "why"}]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"operations": ops}))
    assert cli._load_batch_plan(str(path)) == ops


def test_op_reason_missing(cli):
    with pytest.raises(SystemExit, match="non-empty reason"):
        cli._op_reason({})


def test_op_reason_blank(cli):
    with pytest.raises(SystemExit, match="non-empty reason"):
        cli._op_reason({"reason": "   "})


def test_op_reason_strips_whitespace(cli):
    assert cli._op_reason({"reason": "  why  "}) == "why"


class TestNormalizeUpdateTask:
    def test_field_new_shorthand(self, cli):
        op = cli._normalize_batch_op({"task": "1", "field": "due_on", "new": "2026-07-20", "reason": "r"})
        assert op == {
            "action": "update_task", "source_action": "update_task",
            "task": "1", "fields": {"due_on": "2026-07-20"}, "reason": "r",
        }

    def test_fields_dict(self, cli):
        op = cli._normalize_batch_op({"task": "1", "fields": {"name": "n", "completed": True}, "reason": "r"})
        assert op["fields"] == {"name": "n", "completed": True}

    def test_missing_task_or_fields(self, cli):
        with pytest.raises(SystemExit, match="need task plus fields"):
            cli._normalize_batch_op({"fields": {"name": "n"}, "reason": "r"})
        with pytest.raises(SystemExit, match="need task plus fields"):
            cli._normalize_batch_op({"task": "1", "reason": "r"})

    def test_unsupported_field(self, cli):
        with pytest.raises(SystemExit, match="unsupported update_task field"):
            cli._normalize_batch_op({"task": "1", "field": "assignee", "new": "x", "reason": "r"})


class TestNormalizeAliases:
    def test_rename(self, cli):
        op = cli._normalize_batch_op({"action": "rename", "task": "1", "name": "New", "reason": "r"})
        assert op == {
            "action": "update_task", "source_action": "rename",
            "task": "1", "fields": {"name": "New"}, "reason": "r",
        }

    def test_rename_missing_name(self, cli):
        with pytest.raises(SystemExit, match="rename operations need task and name"):
            cli._normalize_batch_op({"action": "rename", "task": "1", "reason": "r"})

    def test_set_notes(self, cli):
        op = cli._normalize_batch_op({"action": "set_notes", "task": "1", "notes": "hi", "reason": "r"})
        assert op["fields"] == {"notes": "hi"}

    def test_set_notes_allows_empty_string(self, cli):
        op = cli._normalize_batch_op({"action": "set_notes", "task": "1", "notes": "", "reason": "r"})
        assert op["fields"] == {"notes": ""}

    def test_set_notes_missing_notes_key(self, cli):
        with pytest.raises(SystemExit, match="set_notes operations need task and notes"):
            cli._normalize_batch_op({"action": "set_notes", "task": "1", "reason": "r"})


class TestNormalizeReplaceNotes:
    def test_valid(self, cli):
        op = cli._normalize_batch_op({"action": "replace_notes", "task": "1", "old": "a", "new": "b", "reason": "r"})
        assert op == {
            "action": "replace_notes", "source_action": "replace_notes",
            "task": "1", "old": "a", "new": "b", "reason": "r",
        }

    def test_empty_old_rejected(self, cli):
        with pytest.raises(SystemExit, match="replace_notes operations need task plus non-empty old"):
            cli._normalize_batch_op({"action": "replace_notes", "task": "1", "old": "", "new": "b", "reason": "r"})

    def test_non_string_new_rejected(self, cli):
        with pytest.raises(SystemExit, match="replace_notes operations need task plus non-empty old"):
            cli._normalize_batch_op({"action": "replace_notes", "task": "1", "old": "a", "new": None, "reason": "r"})


class TestNormalizeMove:
    def test_valid(self, cli):
        op = cli._normalize_batch_op({"action": "move", "task": "1", "section": "2", "reason": "r"})
        assert op == {"action": "move", "task": "1", "section": "2", "reason": "r"}

    def test_missing_section(self, cli):
        with pytest.raises(SystemExit, match="move operations need task and section"):
            cli._normalize_batch_op({"action": "move", "task": "1", "reason": "r"})


class TestNormalizeCreateTask:
    def test_valid_minimal(self, cli):
        op = cli._normalize_batch_op({"action": "create_task", "project": "1", "name": "n", "reason": "r"})
        assert op == {
            "action": "create_task", "source_action": "create_task",
            "project": "1", "name": "n", "section": None, "notes": None, "reason": "r",
        }

    def test_missing_project(self, cli):
        with pytest.raises(SystemExit, match="create_task operations need project and name"):
            cli._normalize_batch_op({"action": "create_task", "name": "n", "reason": "r"})


class TestNormalizeCreateSubtask:
    def test_valid(self, cli):
        op = cli._normalize_batch_op({"action": "create_subtask", "parent": "1", "name": "n", "reason": "r"})
        assert op["parent"] == "1" and op["name"] == "n"

    def test_missing_parent(self, cli):
        with pytest.raises(SystemExit, match="create_subtask operations need parent and name"):
            cli._normalize_batch_op({"action": "create_subtask", "name": "n", "reason": "r"})


class TestNormalizeAddComment:
    def test_valid(self, cli):
        assert cli._normalize_batch_op({
            "action": "add_comment", "task": "1", "text": "legacy", "reason": "r",
        }) == {
            "action": "add_comment", "task": "1", "text": "legacy", "reason": "r",
        }

    @pytest.mark.parametrize("op", [
        {"action": "add_comment", "text": "x", "reason": "r"},
        {"action": "add_comment", "task": "1", "text": "", "reason": "r"},
        {"action": "add_comment", "task": "1", "text": None, "reason": "r"},
    ])
    def test_missing_target_or_text(self, cli, op):
        with pytest.raises(SystemExit, match="add_comment operations need task and non-empty text"):
            cli._normalize_batch_op(op)


def test_normalize_unknown_action(cli):
    with pytest.raises(SystemExit, match="unsupported batch action"):
        cli._normalize_batch_op({"action": "delete", "task": "1", "reason": "r"})


def test_normalize_non_dict_op(cli):
    with pytest.raises(SystemExit, match="each batch operation must be an object"):
        cli._normalize_batch_op("not a dict")
