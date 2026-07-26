"""Single-item command coverage: c_get, c_notes, c_set_notes, c_append,
c_replace, c_rename, c_project, c_move, c_create_task, c_create_subtask.

Previously only batch-apply, pagination, raw, decode, and transport-level
error mapping were tested -- these are the everyday single-task commands an
agent or user actually runs, and had zero regression protection.
"""
import asana
import pytest


def test_get_prints_name_and_notes(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {"name": "Buy milk", "notes": "2%", "completed": False}},
    )
    cli.c_get("1")
    out = capsys.readouterr().out
    assert "Buy milk" in out
    assert "2%" in out
    assert "[completed]" not in out


def test_get_marks_completed_task(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {"name": "Done thing", "notes": "", "completed": True}},
    )
    cli.c_get("1")
    out = capsys.readouterr().out
    assert "[completed]" in out


def test_notes_prints_notes_only(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {"notes": "just the notes"}},
    )
    cli.c_notes("1")
    out = capsys.readouterr().out
    assert out.strip() == "just the notes"


def test_set_notes_sends_decoded_text(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        asana.TasksApi, "update_task",
        lambda self, body, task_gid, opts, **kw: calls.append((body, task_gid)) or {"data": {}},
    )
    cli.c_set_notes("1", "line1\\nline2")
    assert calls == [({"data": {"notes": "line1\nline2"}}, "1")]


def test_append_reads_current_notes_then_appends(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {"notes": "existing"}},
    )
    monkeypatch.setattr(
        asana.TasksApi, "update_task",
        lambda self, body, task_gid, opts, **kw: calls.append((body, task_gid)) or {"data": {}},
    )
    cli.c_append("1", " more")
    assert calls == [({"data": {"notes": "existing more"}}, "1")]


def test_append_treats_missing_notes_as_empty(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {}},
    )
    monkeypatch.setattr(
        asana.TasksApi, "update_task",
        lambda self, body, task_gid, opts, **kw: calls.append((body, task_gid)) or {"data": {}},
    )
    cli.c_append("1", "first note")
    assert calls == [({"data": {"notes": "first note"}}, "1")]


def test_replace_requires_exactly_one_match(cli, monkeypatch):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {"notes": "foo foo"}},
    )
    with pytest.raises(SystemExit) as exc:
        cli.c_replace("1", "foo", "bar")
    assert "2" in str(exc.value)


def test_replace_applies_exact_match(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {"notes": "hello world"}},
    )
    monkeypatch.setattr(
        asana.TasksApi, "update_task",
        lambda self, body, task_gid, opts, **kw: calls.append((body, task_gid)) or {"data": {}},
    )
    cli.c_replace("1", "world", "there")
    assert calls == [({"data": {"notes": "hello there"}}, "1")]


def test_rename_sends_new_name(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        asana.TasksApi, "update_task",
        lambda self, body, task_gid, opts, **kw: calls.append((body, task_gid)) or {"data": {}},
    )
    cli.c_rename("1", "New name")
    assert calls == [({"data": {"name": "New name"}}, "1")]


def test_project_prints_name_and_notes(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        asana.ProjectsApi, "get_project",
        lambda self, gid, opts, **kw: {"data": {"name": "Kitchen remodel", "notes": "phase 1"}},
    )
    cli.c_project("1")
    out = capsys.readouterr().out
    assert "Kitchen remodel" in out
    assert "phase 1" in out


def test_move_adds_task_to_section(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        asana.SectionsApi, "add_task_for_section",
        lambda self, section_gid, opts, **kw: calls.append((opts["body"], section_gid)) or {"data": {}},
    )
    cli.c_move("1", "999")
    assert calls == [({"data": {"task": "1"}}, "999")]


def test_create_task_prints_gid(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        asana.TasksApi, "create_task",
        lambda self, body, opts, **kw: {"data": {"gid": "new-1"}},
    )
    cli.c_create_task("proj-1", "New task", None, None)
    out = capsys.readouterr().out
    assert out.strip() == "new-1"


def test_create_task_includes_notes_when_given(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(
        asana.TasksApi, "create_task",
        lambda self, body, opts, **kw: calls.append(body) or {"data": {"gid": "new-1"}},
    )
    cli.c_create_task("proj-1", "New task", None, "some notes")
    assert calls[0]["data"]["notes"] == "some notes"


def test_create_task_moves_to_section_when_given(cli, monkeypatch):
    move_calls = []
    monkeypatch.setattr(
        asana.TasksApi, "create_task",
        lambda self, body, opts, **kw: {"data": {"gid": "new-1"}},
    )
    monkeypatch.setattr(
        asana.SectionsApi, "add_task_for_section",
        lambda self, section_gid, opts, **kw: move_calls.append((opts["body"], section_gid)) or {"data": {}},
    )
    cli.c_create_task("proj-1", "New task", "sec-1", None)
    assert move_calls == [({"data": {"task": "new-1"}}, "sec-1")]


def test_create_subtask_prints_gid(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        asana.TasksApi, "create_subtask_for_task",
        lambda self, body, parent_gid, opts, **kw: {"data": {"gid": "sub-1"}},
    )
    cli.c_create_subtask("parent-1", "Sub task", None)
    out = capsys.readouterr().out
    assert out.strip() == "sub-1"
