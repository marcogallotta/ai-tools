"""Explicit, one-page-per-call pagination at the CLI surface (asana-cli-fixes.md P1 #3/#4).

None of this exists yet: `tasks`/`subtasks`/`sections`/`search`/`projects` have
no --cursor flag, and `tasks` still uses the old --all boolean and section/
project fallback guessing rather than the `tasks section|project <gid>`
syntax. These tests describe the intended surface and are expected to fail
until P1 #3/#4 land.
"""
import asana
from asana.rest import ApiException
import pytest

PAGE_ONE = [{"gid": "1", "name": "First", "completed": False}]
PAGE_ONE_MORE = {"data": PAGE_ONE, "next_page": {"offset": "next-token-abc"}}
PAGE_LAST = {"data": PAGE_ONE, "next_page": None}


def _mock_list(monkeypatch, api_cls, method_name, result):
    calls = []

    def fake(self, *args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(api_cls, method_name, fake)
    return calls


CASES = [
    pytest.param(
        ["tasks", "section", "111"], asana.TasksApi, "get_tasks_for_section",
        id="tasks-section",
    ),
    pytest.param(
        ["tasks", "project", "111"], asana.TasksApi, "get_tasks_for_project",
        id="tasks-project",
    ),
    pytest.param(
        ["subtasks", "222"], asana.TasksApi, "get_subtasks_for_task",
        id="subtasks",
    ),
    pytest.param(
        ["sections", "333"], asana.SectionsApi, "get_sections_for_project",
        id="sections",
    ),
    pytest.param(
        ["search", "task", "coffee"], asana.TypeaheadApi, "typeahead_for_workspace",
        id="search",
    ),
    pytest.param(
        ["projects"], asana.ProjectsApi, "get_projects",
        id="projects",
    ),
]


@pytest.mark.parametrize("argv,api_cls,method_name", CASES)
def test_single_call_fetches_exactly_one_page(cli, run, monkeypatch, argv, api_cls, method_name):
    calls = _mock_list(monkeypatch, api_cls, method_name, PAGE_LAST)
    run(cli, argv)
    assert len(calls) == 1


@pytest.mark.parametrize("argv,api_cls,method_name", CASES)
def test_page_size_always_100(cli, run, monkeypatch, argv, api_cls, method_name, capsys):
    calls = _mock_list(monkeypatch, api_cls, method_name, PAGE_LAST)
    run(cli, argv)
    args, kwargs = calls[0]
    opts = args[-1] if args and isinstance(args[-1], dict) else kwargs.get("opts", {})
    assert opts.get("limit") == 100


@pytest.mark.parametrize("argv,api_cls,method_name", CASES)
def test_cursor_printed_when_more_results_exist(cli, run, monkeypatch, argv, api_cls, method_name, capsys):
    _mock_list(monkeypatch, api_cls, method_name, PAGE_ONE_MORE)
    run(cli, argv)
    out = capsys.readouterr().out
    assert "next-token-abc" in out


@pytest.mark.parametrize("argv,api_cls,method_name", CASES)
def test_no_cursor_printed_on_last_page(cli, run, monkeypatch, argv, api_cls, method_name, capsys):
    _mock_list(monkeypatch, api_cls, method_name, PAGE_LAST)
    run(cli, argv)
    out = capsys.readouterr().out
    assert "next-token-abc" not in out


@pytest.mark.parametrize("argv,api_cls,method_name", CASES)
def test_cursor_flag_forwarded_verbatim(cli, run, monkeypatch, argv, api_cls, method_name):
    calls = _mock_list(monkeypatch, api_cls, method_name, PAGE_LAST)
    run(cli, argv + ["--cursor", "prior-token-xyz"])
    args, kwargs = calls[0]
    opts = args[-1] if args and isinstance(args[-1], dict) else kwargs.get("opts", {})
    assert opts.get("offset") == "prior-token-xyz"


STATUS_CASES = [c for c in CASES if c.id in ("tasks-section", "tasks-project", "subtasks")]


@pytest.mark.parametrize("argv,api_cls,method_name", STATUS_CASES)
def test_status_defaults_to_incomplete(cli, run, monkeypatch, argv, api_cls, method_name, capsys):
    result = {
        "data": [
            {"gid": "1", "name": "Open", "completed": False},
            {"gid": "2", "name": "Done", "completed": True},
        ],
        "next_page": None,
    }
    _mock_list(monkeypatch, api_cls, method_name, result)
    run(cli, argv)
    out = capsys.readouterr().out
    assert "Open" in out
    assert "Done" not in out


@pytest.mark.parametrize("argv,api_cls,method_name", STATUS_CASES)
def test_status_both_includes_complete_and_incomplete(cli, run, monkeypatch, argv, api_cls, method_name, capsys):
    result = {
        "data": [
            {"gid": "1", "name": "Open", "completed": False},
            {"gid": "2", "name": "Done", "completed": True},
        ],
        "next_page": None,
    }
    _mock_list(monkeypatch, api_cls, method_name, result)
    run(cli, argv + ["--status", "both"])
    out = capsys.readouterr().out
    assert "Open" in out
    assert "Done" in out


@pytest.mark.parametrize("argv,api_cls,method_name", STATUS_CASES)
def test_status_complete_shows_only_completed(cli, run, monkeypatch, argv, api_cls, method_name, capsys):
    result = {
        "data": [
            {"gid": "1", "name": "Open", "completed": False},
            {"gid": "2", "name": "Done", "completed": True},
        ],
        "next_page": None,
    }
    _mock_list(monkeypatch, api_cls, method_name, result)
    run(cli, argv + ["--status", "complete"])
    out = capsys.readouterr().out
    assert "Open" not in out
    assert "Done" in out


TASKS_ONLY_CASES = [c for c in CASES if c.id in ("tasks-section", "tasks-project")]


@pytest.mark.parametrize("argv,api_cls,method_name", TASKS_ONLY_CASES)
def test_incomplete_status_filters_server_side(cli, run, monkeypatch, argv, api_cls, method_name):
    """tasks section|project supports completed_since; default --status
    incomplete should send completed_since=now so an incomplete-only page
    isn't diluted by completed tasks Asana already knows to exclude."""
    calls = _mock_list(monkeypatch, api_cls, method_name, PAGE_LAST)
    run(cli, argv)
    args, kwargs = calls[0]
    opts = args[-1] if args and isinstance(args[-1], dict) else kwargs.get("opts", {})
    assert opts.get("completed_since") == "now"


@pytest.mark.parametrize("argv,api_cls,method_name", TASKS_ONLY_CASES)
def test_non_incomplete_status_omits_completed_since(cli, run, monkeypatch, argv, api_cls, method_name):
    calls = _mock_list(monkeypatch, api_cls, method_name, PAGE_LAST)
    run(cli, argv + ["--status", "both"])
    args, kwargs = calls[0]
    opts = args[-1] if args and isinstance(args[-1], dict) else kwargs.get("opts", {})
    assert "completed_since" not in opts


def test_subtasks_does_not_send_completed_since(cli, run, monkeypatch):
    """The subtasks endpoint has no completed_since param (unlike
    get_tasks_for_{section,project}) -- must never send it."""
    calls = _mock_list(monkeypatch, asana.TasksApi, "get_subtasks_for_task", PAGE_LAST)
    run(cli, ["subtasks", "222"])
    args, kwargs = calls[0]
    opts = args[-1] if args and isinstance(args[-1], dict) else kwargs.get("opts", {})
    assert "completed_since" not in opts


@pytest.mark.parametrize("cmd_argv", [["tasks", "section", "111"], ["tasks", "project", "111"], ["subtasks", "222"]])
def test_invalid_status_rejected(cli, run, monkeypatch, cmd_argv):
    with pytest.raises(SystemExit):
        run(cli, cmd_argv + ["--status", "complet"])


def test_tasks_bare_gid_no_longer_accepted(cli, run, monkeypatch):
    """The old type-guessing `tasks <gid>` form is removed in favor of
    `tasks section <gid>` / `tasks project <gid>`. Both endpoints are mocked
    to succeed so a SystemExit here can only mean the bare-gid form itself
    was rejected, not a network/auth failure."""
    _mock_list(monkeypatch, asana.TasksApi, "get_tasks_for_section", PAGE_LAST)
    _mock_list(monkeypatch, asana.TasksApi, "get_tasks_for_project", PAGE_LAST)
    with pytest.raises(SystemExit):
        run(cli, ["tasks", "111"])


def _api_exc(status=404, reason="Not Found", body=b"{}"):
    e = ApiException(status=status, reason=reason)
    e.body = body
    return e


def test_tasks_section_failure_does_not_fall_back_to_project(cli, run, monkeypatch):
    """`tasks section <gid>` must fail loudly on a real API error, not
    silently retry against the project endpoint (the fallback guessing P1 #4
    removes)."""
    monkeypatch.setattr(
        asana.TasksApi, "get_tasks_for_section",
        lambda self, *a, **kw: (_ for _ in ()).throw(_api_exc()),
    )
    project_calls = _mock_list(monkeypatch, asana.TasksApi, "get_tasks_for_project", PAGE_LAST)

    with pytest.raises(SystemExit):
        run(cli, ["tasks", "section", "111"])

    assert project_calls == []


def test_tasks_project_failure_does_not_fall_back_to_section(cli, run, monkeypatch):
    """`tasks project <gid>` must fail loudly on a real API error, not
    silently retry against the section endpoint."""
    section_calls = _mock_list(monkeypatch, asana.TasksApi, "get_tasks_for_section", PAGE_LAST)
    monkeypatch.setattr(
        asana.TasksApi, "get_tasks_for_project",
        lambda self, *a, **kw: (_ for _ in ()).throw(_api_exc()),
    )

    with pytest.raises(SystemExit):
        run(cli, ["tasks", "project", "111"])

    assert section_calls == []


GID_CASES = [c for c in CASES if c.id in ("tasks-section", "tasks-project", "subtasks", "sections")]


@pytest.mark.parametrize("argv,api_cls,method_name", GID_CASES)
def test_404_names_the_resource_gid(cli, run, monkeypatch, argv, api_cls, method_name):
    """Like the single-item lookups (test_transport.py::test_404_names_the_resource),
    a 404 from a gid-scoped list endpoint must name the gid it was fetching,
    not just a bare status/body -- otherwise a failing --cursor loop over
    several gids is impossible to attribute."""
    monkeypatch.setattr(
        api_cls, method_name,
        lambda self, *a, **kw: (_ for _ in ()).throw(_api_exc()),
    )
    gid = argv[-1]

    with pytest.raises(SystemExit) as exc:
        run(cli, argv)

    msg = str(exc.value)
    assert "404" in msg
    assert gid in msg


def test_projects_modified_since_filters_locally(cli, run, monkeypatch, capsys):
    """/projects has no server-side modified_since param, so filtering must
    happen client-side per page (same pattern as --status on subtasks)."""
    page = {
        "data": [
            {"gid": "1", "name": "Old", "archived": False, "modified_at": "2026-07-01T00:00:00.000Z"},
            {"gid": "2", "name": "New", "archived": False, "modified_at": "2026-07-22T00:00:00.000Z"},
        ],
        "next_page": None,
    }
    calls = _mock_list(monkeypatch, asana.ProjectsApi, "get_projects", page)
    run(cli, ["projects", "--modified-since", "2026-07-20"])
    out = capsys.readouterr().out
    assert "New" in out
    assert "Old" not in out
    args, kwargs = calls[0]
    opts = args[-1] if args and isinstance(args[-1], dict) else kwargs.get("opts", {})
    assert "modified_at" in opts.get("opt_fields", "")


def test_projects_without_modified_since_does_not_request_modified_at(cli, run, monkeypatch):
    """Avoid asking the API for a field nothing will use when the flag is absent."""
    calls = _mock_list(monkeypatch, asana.ProjectsApi, "get_projects", PAGE_LAST)
    run(cli, ["projects"])
    args, kwargs = calls[0]
    opts = args[-1] if args and isinstance(args[-1], dict) else kwargs.get("opts", {})
    assert "modified_at" not in opts.get("opt_fields", "")
