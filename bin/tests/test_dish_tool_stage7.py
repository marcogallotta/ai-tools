"""Stage 7: advisory integration for generic Asana note mutations."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN_DIR))

import asana  # noqa: E402
import pytest  # noqa: E402

import dish_tool.advisory as advisory_module  # noqa: E402
from dish_tool.advisory import AdvisoryGuard  # noqa: E402
from dish_tool.constants import (  # noqa: E402
    COOKING_PROJECT_GID,
    REFERENCE_SECTION_GID,
    SOURCING_SECTION_GID,
)
from dish_tool.database import initialize_database  # noqa: E402


SECTIONS = [
    {"gid": "research", "name": "Research Queue"},
    {"gid": "verification", "name": "Verification Queue"},
    {"gid": SOURCING_SECTION_GID, "name": "Sourcing"},
    {"gid": REFERENCE_SECTION_GID, "name": "Reference"},
    {"gid": "managed", "name": "Weeknight"},
]


def _task(task_gid: str, section_gid: str | None, *, section_name: str = "ignored") -> dict:
    section = None if section_gid is None else {"gid": section_gid, "name": section_name}
    return {
        "gid": task_gid,
        "memberships": [
            {
                "project": {"gid": COOKING_PROJECT_GID, "name": "Cooking"},
                "section": section,
            }
        ],
    }


def _install_guard(cli, monkeypatch, tmp_path: Path, tasks: dict[str, dict], *, agent: str | None = None):
    db_path = tmp_path / "dish.db"
    if agent is None:
        monkeypatch.delenv("ASANA_AGENT", raising=False)
    else:
        monkeypatch.setenv("ASANA_AGENT", agent)

    def get_task(self, task_gid, opts, **kwargs):
        task = dict(tasks[task_gid])
        if "notes" in opts.get("opt_fields", ""):
            task.setdefault("notes", "existing")
        return {"data": task}

    monkeypatch.setattr(asana.TasksApi, "get_task", get_task)
    monkeypatch.setattr(
        asana.SectionsApi,
        "get_sections_for_project",
        lambda self, project_gid, opts, **kwargs: {"data": list(SECTIONS)},
    )
    initialize_database(db_path).close()
    cli._ADVISORY_GUARD = AdvisoryGuard(api_client=object(), db_path=db_path)
    return db_path


def _events(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT task_gid, event_type, actor_agent, details FROM audit_events ORDER BY rowid"
        ).fetchall()


@pytest.mark.parametrize("command", ["set-notes", "append", "replace"])
def test_direct_note_mutations_log_managed_bypass_and_proceed(
    cli, monkeypatch, tmp_path, command
):
    db_path = _install_guard(
        cli, monkeypatch, tmp_path, {"task-1": _task("task-1", "managed")}, agent="codex"
    )
    writes = []
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: writes.append((task_gid, body))
        or {"data": {"gid": task_gid}},
    )

    if command == "set-notes":
        cli.c_set_notes("task-1", "new")
    elif command == "append":
        cli.c_append("task-1", " plus")
    else:
        cli.c_replace("task-1", "existing", "new")

    assert len(writes) == 1
    rows = _events(db_path)
    assert len(rows) == 1
    assert rows[0]["task_gid"] == "task-1"
    assert rows[0]["event_type"] == "generic_note_bypass"
    assert rows[0]["actor_agent"] == "codex"
    details = json.loads(rows[0]["details"])
    assert details["command"] == command
    assert details["resolution"] == "managed_section"


def test_excluded_gid_remains_excluded_when_display_name_changes(cli, monkeypatch, tmp_path):
    db_path = _install_guard(
        cli,
        monkeypatch,
        tmp_path,
        {"task-1": _task("task-1", SOURCING_SECTION_GID, section_name="Ingredients Inbox")},
    )
    writes = []
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: writes.append(task_gid)
        or {"data": {"gid": task_gid}},
    )

    cli.c_set_notes("task-1", "new")

    assert writes == ["task-1"]
    assert _events(db_path) == []


def test_pinned_excluded_gid_survives_rename_across_cli_invocations(cli, monkeypatch, tmp_path):
    db_path = tmp_path / "dish.db"
    initialize_database(db_path).close()
    tasks = {
        "managed-1": _task("managed-1", "managed"),
        "excluded-1": _task(
            "excluded-1", SOURCING_SECTION_GID, section_name="Ingredients Inbox"
        ),
    }

    def get_task(self, task_gid, opts, **kwargs):
        return {"data": dict(tasks[task_gid])}

    monkeypatch.setattr(asana.TasksApi, "get_task", get_task)
    monkeypatch.setattr(
        asana.SectionsApi,
        "get_sections_for_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pinned exclusions must not depend on section names")
        ),
    )
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: {"data": {"gid": task_gid}},
    )
    cli._ADVISORY_GUARD = AdvisoryGuard(api_client=object(), db_path=db_path)

    cli.c_set_notes("managed-1", "first")
    cli._ADVISORY_GUARD = AdvisoryGuard(api_client=object(), db_path=db_path)
    cli.c_set_notes("excluded-1", "second")

    rows = _events(db_path)
    assert len(rows) == 1
    assert rows[0]["task_gid"] == "managed-1"


def test_unresolved_cooking_membership_logs_bypass_and_write_continues(cli, monkeypatch, tmp_path):
    db_path = _install_guard(
        cli, monkeypatch, tmp_path, {"task-1": _task("task-1", None)}
    )
    writes = []
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: writes.append(task_gid)
        or {"data": {"gid": task_gid}},
    )

    cli.c_set_notes("task-1", "new")

    assert writes == ["task-1"]
    details = json.loads(_events(db_path)[0]["details"])
    assert details["resolution"] == "section_unresolved"


def test_task_outside_cooking_is_not_managed(cli, monkeypatch, tmp_path):
    outside = {"gid": "task-1", "memberships": [{"project": {"gid": "other"}, "section": None}]}
    db_path = _install_guard(cli, monkeypatch, tmp_path, {"task-1": outside})
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: {"data": {"gid": task_gid}},
    )

    cli.c_set_notes("task-1", "new")

    assert _events(db_path) == []


def test_note_bearing_creation_checks_intended_section(cli, monkeypatch, tmp_path):
    db_path = _install_guard(cli, monkeypatch, tmp_path, {})
    creates = []
    monkeypatch.setattr(
        asana.TasksApi,
        "create_task",
        lambda self, body, opts, **kwargs: creates.append(body) or {"data": {"gid": "new-1"}},
    )
    monkeypatch.setattr(
        asana.SectionsApi,
        "add_task_for_section",
        lambda self, body, section_gid, opts, **kwargs: {"data": {}},
    )

    cli.c_create_task(COOKING_PROJECT_GID, "Managed", "managed", "notes")
    cli.c_create_task(COOKING_PROJECT_GID, "Excluded", REFERENCE_SECTION_GID, "notes")
    cli.c_create_task(COOKING_PROJECT_GID, "Unresolved", None, "notes")
    cli.c_create_task("other-project", "Other", None, "notes")

    assert len(creates) == 4
    rows = _events(db_path)
    assert len(rows) == 2
    assert [json.loads(row["details"])["resolution"] for row in rows] == [
        "managed_section",
        "section_unresolved",
    ]
    assert all(row["task_gid"] is None for row in rows)


def test_bare_creation_and_non_note_write_do_not_consult_or_log(cli, monkeypatch, tmp_path):
    db_path = _install_guard(cli, monkeypatch, tmp_path, {"task-1": _task("task-1", "managed")})
    monkeypatch.setattr(
        asana.TasksApi,
        "create_task",
        lambda self, body, opts, **kwargs: {"data": {"gid": "new-1"}},
    )
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: {"data": {"gid": task_gid}},
    )
    monkeypatch.setattr(
        asana.SectionsApi,
        "add_task_for_section",
        lambda self, body, section_gid, opts, **kwargs: {"data": {}},
    )

    cli.c_create_task(COOKING_PROJECT_GID, "Bare", "managed", None)
    cli.c_rename("task-1", "Renamed")

    assert _events(db_path) == []


def _write_plan(tmp_path: Path, operations: list[dict]) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"operations": operations}))
    return str(path)


def test_batch_logs_each_note_bearing_operation_only(cli, monkeypatch, tmp_path):
    tasks = {
        "managed-1": _task("managed-1", "managed"),
        "excluded-1": _task("excluded-1", REFERENCE_SECTION_GID),
    }
    db_path = _install_guard(cli, monkeypatch, tmp_path, tasks)
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: {"data": {"gid": task_gid}},
    )
    monkeypatch.setattr(
        asana.TasksApi,
        "create_task",
        lambda self, body, opts, **kwargs: {"data": {"gid": "new-1"}},
    )
    monkeypatch.setattr(
        asana.SectionsApi,
        "add_task_for_section",
        lambda self, body, section_gid, opts, **kwargs: {"data": {}},
    )

    path = _write_plan(
        tmp_path,
        [
            {"action": "set_notes", "task": "managed-1", "notes": "a", "reason": "x"},
            {"action": "update_task", "task": "managed-1", "fields": {"notes": "b"}, "reason": "x"},
            {"action": "replace_notes", "task": "managed-1", "old": "existing", "new": "c", "reason": "x"},
            {"action": "update_task", "task": "excluded-1", "fields": {"notes": "d"}, "reason": "x"},
            {"action": "update_task", "task": "managed-1", "fields": {"name": "n"}, "reason": "x"},
            {
                "action": "create_task",
                "project": COOKING_PROJECT_GID,
                "section": "managed",
                "name": "new",
                "notes": "c",
                "reason": "x",
            },
        ],
    )

    cli.c_batch_apply(path)

    rows = _events(db_path)
    assert len(rows) == 4
    details = [json.loads(row["details"]) for row in rows]
    assert [item["command"] for item in details] == ["batch-apply"] * 4
    assert [item["operation"] for item in details] == [
        "set_notes",
        "update_task",
        "replace_notes",
        "create_task",
    ]


@pytest.mark.parametrize("field", ["notes", "html_notes"])
def test_raw_task_note_fields_are_advised(cli, monkeypatch, tmp_path, field):
    db_path = _install_guard(
        cli, monkeypatch, tmp_path, {"task-1": _task("task-1", "managed")}
    )
    calls = []
    monkeypatch.setattr(
        asana.ApiClient,
        "call_api",
        lambda self, path, method, *args, **kwargs: calls.append((path, method, kwargs.get("body")))
        or {"data": {}},
    )
    stdin = io.StringIO(json.dumps({field: "new"}))
    stdin.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", stdin)

    cli.c_raw("PUT", "/tasks/task-1")

    assert len(calls) == 1
    details = json.loads(_events(db_path)[0]["details"])
    assert details["command"] == "raw"
    assert details["fields"] == [field]


def test_raw_non_note_write_is_not_advised(cli, monkeypatch, tmp_path):
    db_path = _install_guard(
        cli, monkeypatch, tmp_path, {"task-1": _task("task-1", "managed")}
    )
    monkeypatch.setattr(
        asana.ApiClient,
        "call_api",
        lambda self, path, method, *args, **kwargs: {"data": {}},
    )
    stdin = io.StringIO(json.dumps({"name": "new"}))
    stdin.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", stdin)

    cli.c_raw("PUT", "/tasks/task-1")

    assert _events(db_path) == []


def test_raw_note_bearing_task_creation_uses_intended_cooking_section(cli, monkeypatch, tmp_path):
    db_path = _install_guard(cli, monkeypatch, tmp_path, {})
    monkeypatch.setattr(
        asana.ApiClient,
        "call_api",
        lambda self, path, method, *args, **kwargs: {"data": {"gid": "new-1"}},
    )
    stdin = io.StringIO(
        json.dumps(
            {
                "name": "new",
                "projects": [COOKING_PROJECT_GID],
                "section": "managed",
                "notes": "body",
            }
        )
    )
    stdin.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", stdin)

    cli.c_raw("POST", "/tasks")

    rows = _events(db_path)
    assert len(rows) == 1
    assert rows[0]["task_gid"] is None
    details = json.loads(rows[0]["details"])
    assert details["command"] == "raw"
    assert details["resolution"] == "managed_section"


def test_failed_replace_validation_does_not_log_bypass(cli, monkeypatch, tmp_path):
    db_path = _install_guard(
        cli, monkeypatch, tmp_path, {"task-1": _task("task-1", "managed")}
    )
    with pytest.raises(SystemExit):
        cli.c_replace("task-1", "missing", "new")
    assert _events(db_path) == []


def test_task_lookup_failure_is_unresolved_but_write_still_proceeds(cli, monkeypatch, tmp_path):
    db_path = tmp_path / "dish.db"
    initialize_database(db_path).close()
    monkeypatch.setenv("ASANA_AGENT", "claude")
    monkeypatch.setattr(
        asana.TasksApi,
        "get_task",
        lambda self, task_gid, opts, **kwargs: (_ for _ in ()).throw(RuntimeError("lookup failed")),
    )
    writes = []
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: writes.append(task_gid)
        or {"data": {"gid": task_gid}},
    )
    cli._ADVISORY_GUARD = AdvisoryGuard(api_client=object(), db_path=db_path)

    cli.c_set_notes("task-1", "new")

    assert writes == ["task-1"]
    row = _events(db_path)[0]
    assert row["actor_agent"] == "claude"
    assert json.loads(row["details"])["resolution"] == "task_lookup_unresolved"


def test_advisory_database_failure_does_not_block_write(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(
        asana.TasksApi,
        "get_task",
        lambda self, task_gid, opts, **kwargs: {
            "data": _task(task_gid, "managed")
        },
    )
    monkeypatch.setattr(
        advisory_module,
        "initialize_database",
        lambda path: (_ for _ in ()).throw(sqlite3.OperationalError("unavailable")),
    )
    writes = []
    monkeypatch.setattr(
        asana.TasksApi,
        "update_task",
        lambda self, body, task_gid, opts, **kwargs: writes.append(task_gid)
        or {"data": {"gid": task_gid}},
    )
    cli._ADVISORY_GUARD = AdvisoryGuard(
        api_client=object(), db_path=tmp_path / "dish.db"
    )

    cli.c_set_notes("task-1", "new")

    assert writes == ["task-1"]
