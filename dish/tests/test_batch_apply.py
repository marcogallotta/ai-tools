"""batch-apply stop-on-error reporting (asana-cli-fixes.md P0 #2).

Current c_batch_apply calls _call(), which sys.exit()s immediately via
_fail() on the first ApiException -- there is no per-operation success
reporting and the run-so-far summary is lost when the exit happens. These
tests describe the intended stop-on-error contract (operations before the
failure reported succeeded, the failing operation reported with its real
API error, nothing after it runs) and are expected to fail until that
reporting is implemented.

Report assertions use word-boundary matching on the task gid tied to a
success/failure keyword, rather than a bare substring check -- gids are
short digit strings ("1", "2", "3") that can trivially collide with
unrelated numbers in the output (an HTTP status code, an op count).
"""
import json
import re

import asana
from asana.rest import ApiException
import pytest

SUCCESS_WORDS = re.compile(r"succeed|success|\bok\b|applied|done", re.I)
FAILURE_WORDS = re.compile(r"fail|error|abort", re.I)


def _write_plan(tmp_path, ops):
    plan = {"operations": ops}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return str(path)


def _op(task, name, reason="because"):
    return {"action": "rename", "task": task, "name": name, "reason": reason}


def _lines_mentioning(report, gid):
    """Lines in `report` where gid appears as a whole token, not a substring
    of an unrelated number (e.g. gid "2" inside a "422" status code)."""
    pattern = re.compile(rf"\b{re.escape(gid)}\b")
    return [line for line in report.splitlines() if pattern.search(line)]


def _assert_reported(report, gid, keyword_pattern):
    lines = _lines_mentioning(report, gid)
    assert lines, "no line in report mentions task %r: %r" % (gid, report)
    assert any(keyword_pattern.search(line) for line in lines), (
        "task %r reported, but no line matches %r: %r" % (gid, keyword_pattern.pattern, lines)
    )


def test_operations_before_failure_reported_succeeded(cli, monkeypatch, tmp_path, capsys):
    calls = []

    def fake_update_task(self, body, task_gid, opts, **kw):
        calls.append(task_gid)
        if task_gid == "2":
            e = ApiException(status=422, reason="Invalid")
            e.body = b'{"errors":[{"message":"bad field"}]}'
            raise e
        return {"data": {"gid": task_gid}}

    monkeypatch.setattr(asana.TasksApi, "update_task", fake_update_task)

    plan_path = _write_plan(tmp_path, [_op("1", "A"), _op("2", "B"), _op("3", "C")])

    with pytest.raises(SystemExit) as exc:
        cli.c_batch_apply(plan_path)

    report = str(exc.value) + capsys.readouterr().out
    _assert_reported(report, "1", SUCCESS_WORDS)
    assert calls == ["1", "2"]  # op 3 must not have run


def test_failing_operation_reports_real_api_error(cli, monkeypatch, tmp_path, capsys):
    def fake_update_task(self, body, task_gid, opts, **kw):
        if task_gid == "2":
            e = ApiException(status=422, reason="Invalid")
            e.body = b'{"errors":[{"message":"bad field"}]}'
            raise e
        return {"data": {"gid": task_gid}}

    monkeypatch.setattr(asana.TasksApi, "update_task", fake_update_task)

    plan_path = _write_plan(tmp_path, [_op("1", "A"), _op("2", "B"), _op("3", "C")])

    with pytest.raises(SystemExit) as exc:
        cli.c_batch_apply(plan_path)

    report = str(exc.value) + capsys.readouterr().out
    lines = _lines_mentioning(report, "2")
    assert lines, "no line in report mentions failing task '2': %r" % report
    assert any(FAILURE_WORDS.search(l) for l in lines)
    assert any(("422" in l or "bad field" in l) for l in lines)


def test_operations_after_failure_do_not_run(cli, monkeypatch, tmp_path):
    calls = []

    def fake_update_task(self, body, task_gid, opts, **kw):
        calls.append(task_gid)
        if task_gid == "2":
            e = ApiException(status=422, reason="Invalid")
            e.body = b"{}"
            raise e
        return {"data": {"gid": task_gid}}

    monkeypatch.setattr(asana.TasksApi, "update_task", fake_update_task)

    plan_path = _write_plan(tmp_path, [_op("1", "A"), _op("2", "B"), _op("3", "C")])

    with pytest.raises(SystemExit):
        cli.c_batch_apply(plan_path)

    assert "3" not in calls


def test_summary_accounts_for_every_operation_up_to_failure(cli, monkeypatch, tmp_path, capsys):
    def fake_update_task(self, body, task_gid, opts, **kw):
        if task_gid == "3":
            e = ApiException(status=500, reason="Server Error")
            e.body = b"{}"
            raise e
        return {"data": {"gid": task_gid}}

    monkeypatch.setattr(asana.TasksApi, "update_task", fake_update_task)

    plan_path = _write_plan(tmp_path, [_op("1", "A"), _op("2", "B"), _op("3", "C"), _op("4", "D")])

    with pytest.raises(SystemExit) as exc:
        cli.c_batch_apply(plan_path)

    report = str(exc.value) + capsys.readouterr().out
    _assert_reported(report, "1", SUCCESS_WORDS)
    _assert_reported(report, "2", SUCCESS_WORDS)
    _assert_reported(report, "3", FAILURE_WORDS)
    assert not _lines_mentioning(report, "4")


def test_exit_code_nonzero_when_any_operation_fails(cli, monkeypatch, tmp_path):
    def fake_update_task(self, body, task_gid, opts, **kw):
        e = ApiException(status=500, reason="Server Error")
        e.body = b"{}"
        raise e

    monkeypatch.setattr(asana.TasksApi, "update_task", fake_update_task)

    plan_path = _write_plan(tmp_path, [_op("1", "A"), _op("2", "B"), _op("3", "C")])

    with pytest.raises(SystemExit) as exc:
        cli.c_batch_apply(plan_path)

    assert exc.value.code not in (0, None, "")


def _create_task_op(project, name, section=None, reason="because"):
    op = {"action": "create_task", "project": project, "name": name, "reason": reason}
    if section:
        op["section"] = section
    return op


def test_create_task_reports_gid_when_section_move_fails(cli, monkeypatch, tmp_path, capsys):
    """If create_task succeeds but the follow-up move-to-section fails, the
    task already exists in Asana -- the failure report must surface its gid,
    not just the project it was created under."""
    monkeypatch.setattr(
        asana.TasksApi, "update_task",
        lambda self, body, task_gid, opts, **kw: {"data": {"gid": task_gid}},
    )
    monkeypatch.setattr(
        asana.TasksApi, "create_task",
        lambda self, body, opts, **kw: {"data": {"gid": "new-task-99"}},
    )

    def fake_add_task_for_section(self, section_gid, opts, **kw):
        body = opts["body"]
        e = ApiException(status=404, reason="Not Found")
        e.body = b'{"errors":[{"message":"section not found"}]}'
        raise e

    monkeypatch.setattr(asana.SectionsApi, "add_task_for_section", fake_add_task_for_section)

    plan_path = _write_plan(tmp_path, [
        _op("1", "A"),
        _op("2", "B"),
        _create_task_op("proj-1", "New Task", section="bad-section"),
    ])

    with pytest.raises(SystemExit) as exc:
        cli.c_batch_apply(plan_path)

    report = str(exc.value) + capsys.readouterr().out
    assert "new-task-99" in report
    assert FAILURE_WORDS.search(report)


def _replace_notes_op(task, old, new, reason="because"):
    return {"action": "replace_notes", "task": task, "old": old, "new": new, "reason": reason}


def test_replace_notes_mismatch_reports_created_before_failure(cli, monkeypatch, tmp_path, capsys):
    """A replace_notes text-match abort is not an ApiException -- it must still
    go through the same failure reporting as any other op, including the
    'Created before failure' summary for tasks already created earlier in
    the same batch."""
    monkeypatch.setattr(
        asana.TasksApi, "create_task",
        lambda self, body, opts, **kw: {"data": {"gid": "new-task-1"}},
    )
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, task_gid, opts, **kw: {"data": {"notes": "no match here"}},
    )

    plan_path = _write_plan(tmp_path, [
        _create_task_op("proj-1", "First"),
        _create_task_op("proj-1", "Second"),
        _replace_notes_op("3", "missing text", "replacement"),
    ])

    with pytest.raises(SystemExit) as exc:
        cli.c_batch_apply(plan_path)

    report = str(exc.value) + capsys.readouterr().out
    assert "new-task-1" in report
    assert "Created before failure" in report
    lines = _lines_mentioning(report, "3")
    assert lines, "no line in report mentions the failing replace_notes task: %r" % report
    assert any(FAILURE_WORDS.search(l) for l in lines)
    assert any("found 0 times" in l for l in lines)


def test_unexpected_exception_reports_created_before_propagating(cli, monkeypatch, tmp_path, capsys):
    """A non-ApiException failure (e.g. a bug or unexpected error from the SDK)
    must not silently drop the created-so-far summary -- it should print it
    and then let the real exception propagate."""
    monkeypatch.setattr(
        asana.TasksApi, "create_task",
        lambda self, body, opts, **kw: {"data": {"gid": "new-task-1"}},
    )

    def fake_update_task(self, body, task_gid, opts, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(asana.TasksApi, "update_task", fake_update_task)

    plan_path = _write_plan(tmp_path, [
        _create_task_op("proj-1", "First"),
        _create_task_op("proj-1", "Second"),
        _op("3", "renamed"),
    ])

    with pytest.raises(RuntimeError):
        cli.c_batch_apply(plan_path)

    out = capsys.readouterr().out
    assert "new-task-1" in out
    assert "Created before failure" in out


def test_all_operations_succeed_reports_full_success(cli, monkeypatch, tmp_path, capsys):
    calls: list[str] = []

    def successful_update(self, body, task_gid, opts, **kw):
        calls.append(task_gid)
        return {"data": {"gid": task_gid}}

    monkeypatch.setattr(asana.TasksApi, "update_task", successful_update)

    plan_path = _write_plan(tmp_path, [_op("1", "A"), _op("2", "B"), _op("3", "C")])

    result = cli.c_batch_apply(plan_path)

    assert result is None
    assert calls == ["1", "2", "3"]
    out = capsys.readouterr().out
    for gid in calls:
        _assert_reported(out, gid, SUCCESS_WORDS)
