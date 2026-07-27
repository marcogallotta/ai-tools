"""Tests for command handlers, exercised with the Asana SDK classes replaced by mocks.

Each test explicitly assigns its own mock for whichever asana.*Api class it needs, rather
than relying on state left over from a previous test -- the fake 'asana' module (installed
once into sys.modules by conftest) is shared across the whole test session.
"""
import json

import pytest
from unittest.mock import MagicMock


def fake_api(monkeypatch, cli, api_name, **method_returns):
    """Patch cli.asana.<api_name> to return a MagicMock whose methods return the given values."""
    instance = MagicMock()
    for method, value in method_returns.items():
        getattr(instance, method).return_value = value
    monkeypatch.setattr(cli.asana, api_name, MagicMock(return_value=instance))
    return instance


class TestGetNotes:
    def test_c_get_prints_name_and_notes(self, cli, monkeypatch, capsys):
        fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"name": "Task", "notes": "body"}})
        cli.c_get("123")
        out = capsys.readouterr().out
        assert "Task" in out
        assert "body" in out

    def test_c_get_marks_completed(self, cli, monkeypatch, capsys):
        fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"name": "Task", "notes": "", "completed": True}})
        cli.c_get("123")
        assert "[completed]" in capsys.readouterr().out

    def test_c_notes_prints_notes_only(self, cli, monkeypatch, capsys):
        fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"notes": "just notes"}})
        cli.c_notes("123")
        assert capsys.readouterr().out == "just notes\n"

    def test_c_notes_handles_missing_notes(self, cli, monkeypatch, capsys):
        fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {}})
        cli.c_notes("123")
        assert capsys.readouterr().out == "\n"


class TestWritesInvokeCookingGuard:
    def test_set_notes_calls_guard_before_update(self, cli, monkeypatch, capsys):
        api = fake_api(monkeypatch, cli, "TasksApi", update_task={"data": {}})
        cli.c_set_notes("123", "hello")
        assert api.update_task.called
        assert cli._test_guard.calls[0][0] == "before_task_mutation"
        assert cli._test_guard.calls[0][1] == ("123",)

    def test_rename_calls_guard_with_name_field(self, cli, monkeypatch, capsys):
        fake_api(monkeypatch, cli, "TasksApi", update_task={"data": {}})
        cli.c_rename("123", "New Name")
        call = cli._test_guard.calls[0]
        assert call[0] == "before_task_mutation"
        assert call[2]["fields"] == ("name",)

    def test_append_reads_then_updates(self, cli, monkeypatch):
        api = fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"notes": "old"}}, update_task={"data": {}})
        cli.c_append("123", " more")
        sent = api.update_task.call_args[0][0]
        assert sent["data"]["notes"] == "old more"


class TestReplace:
    def test_replace_requires_exactly_one_match(self, cli, monkeypatch):
        fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"notes": "a b a"}})
        with pytest.raises(SystemExit, match="found 2 times"):
            cli.c_replace("123", "a", "x")

    def test_replace_aborts_on_zero_matches(self, cli, monkeypatch):
        fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"notes": "nothing here"}})
        with pytest.raises(SystemExit, match="found 0 times"):
            cli.c_replace("123", "missing", "x")

    def test_replace_applies_on_single_match(self, cli, monkeypatch):
        api = fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"notes": "a b c"}}, update_task={"data": {}})
        cli.c_replace("123", "b", "x")
        sent = api.update_task.call_args[0][0]
        assert sent["data"]["notes"] == "a x c"


class TestCreateTask:
    def test_create_task_without_section(self, cli, monkeypatch, capsys):
        fake_api(monkeypatch, cli, "TasksApi", create_task={"data": {"gid": "999"}})
        cli.c_create_task("proj1", "New Task", None, None)
        assert capsys.readouterr().out.strip() == "999"
        assert cli._test_guard.calls[0][0] == "before_create_task"

    def test_create_task_with_section_moves_it(self, cli, monkeypatch):
        tasks_api = fake_api(monkeypatch, cli, "TasksApi", create_task={"data": {"gid": "999"}})
        sections_api = fake_api(monkeypatch, cli, "SectionsApi", add_task_for_section={"data": {}})
        cli.c_create_task("proj1", "New Task", "sec1", None)
        sections_api.add_task_for_section.assert_called_once_with(
            "sec1", {"body": {"data": {"task": "999"}}}
        )


class TestBatchApply:
    def _write_plan(self, tmp_path, ops):
        path = tmp_path / "plan.json"
        path.write_text(json.dumps({"operations": ops}))
        return str(path)

    def test_rejects_fewer_than_three_ops(self, cli, tmp_path):
        path = self._write_plan(tmp_path, [{"action": "rename", "task": "1", "name": "x", "reason": "r"}])
        with pytest.raises(SystemExit, match="requires at least 3 operations"):
            cli.c_batch_apply(path)

    def test_applies_three_ops_and_reports_success(self, cli, monkeypatch, tmp_path, capsys):
        tasks_api = fake_api(monkeypatch, cli, "TasksApi", update_task={"data": {}})
        ops = [
            {"action": "rename", "task": "1", "name": "A", "reason": "r"},
            {"action": "rename", "task": "2", "name": "B", "reason": "r"},
            {"action": "rename", "task": "3", "name": "C", "reason": "r"},
        ]
        path = self._write_plan(tmp_path, ops)
        cli.c_batch_apply(path)
        out = capsys.readouterr().out
        assert tasks_api.update_task.call_count == 3
        assert "Asana batch applied: 3 operations." in out

    def test_stops_and_reports_created_on_failure(self, cli, monkeypatch, tmp_path, capsys):
        tasks_api = fake_api(monkeypatch, cli, "TasksApi", create_task={"data": {"gid": "111"}})
        tasks_api.update_task.side_effect = cli.ApiException(status=404, body="not found")
        ops = [
            {"action": "create_task", "project": "p1", "name": "A", "reason": "r"},
            {"action": "rename", "task": "999", "name": "B", "reason": "r"},
            {"action": "rename", "task": "998", "name": "C", "reason": "r"},
        ]
        path = self._write_plan(tmp_path, ops)
        with pytest.raises(SystemExit):
            cli.c_batch_apply(path)
        out = capsys.readouterr().out
        assert "Created before failure: 111" in out


class TestMainDispatch:
    def test_get_dispatches_to_c_get(self, cli, monkeypatch, capsys):
        fake_api(monkeypatch, cli, "TasksApi", get_task={"data": {"name": "T", "notes": "n"}})
        monkeypatch.setattr("sys.argv", ["asana", "get", "123"])
        cli.main()
        assert "T" in capsys.readouterr().out

    def test_unknown_command_exits(self, cli, monkeypatch):
        monkeypatch.setattr("sys.argv", ["asana", "bogus"])
        with pytest.raises(SystemExit, match="unknown command"):
            cli.main()

    def test_missing_argument_reported(self, cli, monkeypatch):
        monkeypatch.setattr("sys.argv", ["asana", "get"])
        with pytest.raises(SystemExit, match="missing argument"):
            cli.main()

    def test_invalid_status_flag_exits(self, cli, monkeypatch):
        monkeypatch.setattr("sys.argv", ["asana", "tasks", "project", "1", "--status", "bogus"])
        with pytest.raises(SystemExit, match="invalid --status"):
            cli.main()

    def test_help_prints_usage(self, cli, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["asana", "help"])
        cli.main()
        assert "Asana API CLI" in capsys.readouterr().out


class TestCookingGuardBlocksBeforeWrite:
    def test_task_mutation_block_prevents_sdk_update(self, cli, monkeypatch):
        from dish_tool.generic_asana_guard import CookingMutationBlocked

        api = fake_api(monkeypatch, cli, "TasksApi", update_task={"data": {}})

        class Blocker:
            def before_task_mutation(self, *args, **kwargs):
                raise CookingMutationBlocked(
                    command="set-notes", resolution="managed_section", task_gid="123"
                )

        monkeypatch.setattr(cli, "cooking_guard", lambda: Blocker())
        with pytest.raises(SystemExit, match="generic Asana mutation blocked"):
            cli.c_set_notes("123", "new")
        api.update_task.assert_not_called()

    def test_move_block_prevents_section_api_call(self, cli, monkeypatch):
        from dish_tool.generic_asana_guard import CookingMutationBlocked

        api = fake_api(monkeypatch, cli, "SectionsApi", add_task_for_section={"data": {}})

        class Blocker:
            def before_move(self, *args, **kwargs):
                raise CookingMutationBlocked(
                    command="move", resolution="managed_section", task_gid="123"
                )

        monkeypatch.setattr(cli, "cooking_guard", lambda: Blocker())
        with pytest.raises(SystemExit, match="generic Asana mutation blocked"):
            cli.c_move("123", "456")
        api.add_task_for_section.assert_not_called()
