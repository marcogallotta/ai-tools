import importlib.util
import json
import sqlite3
import sys
import threading
from importlib.machinery import SourceFileLoader
from pathlib import Path

import asana
import pytest

BIN_DIR = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "protocol-release"
sys.path.insert(0, str(BIN_DIR))

from dish_tool.commands import DishApplication  # noqa: E402
from dish_tool.constants import ALLOWED_ACTIONS_BY_STATE, SUBMISSION_STATES  # noqa: E402
from dish_tool.database import initialize_database  # noqa: E402
from dish_tool.errors import BackendFailure  # noqa: E402
from dish_tool.models import ResolvedRelease  # noqa: E402


SECTIONS = [
    {"gid": "research", "name": "Research Queue"},
    {"gid": "verification", "name": "Verification Queue"},
    {"gid": "sourcing", "name": "Sourcing"},
    {"gid": "reference", "name": "Reference"},
    {"gid": "dest", "name": "Weeknight"},
]

PLANNING_NOTE = """# PLANNING BRIEF
Destination section: Weeknight (123456)
Exemptions: [nutrition-fat] keep the existing fat target
"""

COMPLETE_NOTE = """# DISH
Exemptions: [nutrition-fat] keep the existing fat target
Destination section: Weeknight (123456)
Self-verified: yes
Verification: original verification text
## PROCESS RECORD
"""


def release_fixture() -> ResolvedRelease:
    planning_text = (FIXTURE_DIR / "dish-planning-manifest.json").read_text()
    complete_text = (FIXTURE_DIR / "dish-complete-task-manifest.json").read_text()
    return ResolvedRelease(
        version="fixture-v1",
        commit="fixture-commit",
        root=FIXTURE_DIR,
        protocols={
            "planning": (FIXTURE_DIR / "dish-planning-protocol.md").read_text(),
            "research": (FIXTURE_DIR / "dish-research-protocol.md").read_text(),
            "verification": (FIXTURE_DIR / "dish-verification-protocol.md").read_text(),
        },
        manifests={
            "planning": json.loads(planning_text),
            "complete_task": json.loads(complete_text),
        },
        manifest_texts={
            "planning": planning_text,
            "complete_task": complete_text,
        },
    )


def cooking_task(gid: str, notes: str, section_gid: str = "research") -> dict:
    section_name = next(
        section["name"] for section in SECTIONS if section["gid"] == section_gid
    )
    return {
        "gid": gid,
        "name": f"Task {gid}",
        "notes": notes,
        "completed": False,
        "projects": [{"gid": "1215089183018968", "name": "Cooking"}],
        "memberships": [
            {
                "project": {"gid": "1215089183018968", "name": "Cooking"},
                "section": {"gid": section_gid, "name": section_name},
            }
        ],
    }


class FakeBackend:
    def __init__(self, tasks=None):
        self.tasks = dict(tasks or {})
        self.create_calls = []
        self.read_calls = []
        self.section_calls = []
        self.create_error = None

    def list_sections(self, project_gid):
        self.section_calls.append(project_gid)
        return list(SECTIONS)

    def read_task(self, task_gid):
        self.read_calls.append(task_gid)
        try:
            return dict(self.tasks[task_gid])
        except KeyError as exc:
            error = BackendFailure(
                "BACKEND_REJECTED", "missing", status=404, retryable=False
            )
            raise error from exc

    def create_bare_task(self, *, title, project_gid, section_gid):
        self.create_calls.append(
            {"title": title, "project_gid": project_gid, "section_gid": section_gid}
        )
        if self.create_error:
            raise self.create_error
        gid = f"created-{len(self.create_calls)}"
        task = cooking_task(gid, "")
        self.tasks[gid] = task
        return task


def app(tmp_path, backend):
    conn = initialize_database(tmp_path / "dish.db")
    return DishApplication(conn, backend, release_loader=release_fixture)


def audit_rows(application):
    return application.conn.execute(
        "SELECT event_type, actor_agent, task_gid, submission_id, details "
        "FROM audit_events ORDER BY created_at, rowid"
    ).fetchall()


def test_asana_backend_create_is_bare_and_places_confirmed_gid(monkeypatch):
    from dish_tool.backend import AsanaBackend

    calls = []

    class TasksApi:
        def __init__(self, client):
            self.client = client

        def create_task(self, body, opts, **kwargs):
            calls.append(("create", body, opts))
            return {"data": {"gid": "new-task", "name": "New dish"}}

    class SectionsApi:
        def __init__(self, client):
            self.client = client

        def add_task_for_section(self, body, section_gid, opts, **kwargs):
            calls.append(("move", body, section_gid, opts))
            return {"data": {}}

    monkeypatch.setattr(asana, "TasksApi", TasksApi)
    monkeypatch.setattr(asana, "SectionsApi", SectionsApi)
    task = AsanaBackend(api_client=object()).create_bare_task(
        title="New dish", project_gid="cooking", section_gid="research"
    )

    create_body = calls[0][1]["data"]
    assert create_body == {"name": "New dish", "projects": ["cooking"]}
    assert "notes" not in create_body
    assert calls[1] == (
        "move",
        {"data": {"task": "new-task"}},
        "research",
        {},
    )
    assert task["gid"] == "new-task"
    assert task["notes"] == ""


def test_asana_backend_post_create_failure_is_uncertain(monkeypatch):
    from dish_tool.backend import AsanaBackend

    class TasksApi:
        def __init__(self, client):
            self.client = client

        def create_task(self, body, opts, **kwargs):
            return {"data": {"gid": "created-but-not-placed"}}

    class Rejection(Exception):
        status = 400
        body = "bad section"

    class SectionsApi:
        def __init__(self, client):
            self.client = client

        def add_task_for_section(self, body, section_gid, opts, **kwargs):
            raise Rejection()

    monkeypatch.setattr(asana, "TasksApi", TasksApi)
    monkeypatch.setattr(asana, "SectionsApi", SectionsApi)

    with pytest.raises(BackendFailure) as exc:
        AsanaBackend(api_client=object()).create_bare_task(
            title="New dish", project_gid="cooking", section_gid="research"
        )

    assert exc.value.code == "BACKEND_UNCERTAIN"
    assert exc.value.retryable is False
    assert exc.value.details["task_gid"] == "created-but-not-placed"


def test_create_uses_cooking_research_queue_and_never_supplies_notes(tmp_path):
    backend = FakeBackend()
    application = app(tmp_path, backend)

    result = application.execute("create", agent="claude", title="New dish")

    assert result["ok"] is True
    assert result["task_gid"] == "created-1"
    assert backend.create_calls == [
        {
            "title": "New dish",
            "project_gid": "1215089183018968",
            "section_gid": "research",
        }
    ]
    assert len(audit_rows(application)) == 1


def test_create_preserves_clear_vs_ambiguous_failure_and_does_not_retry(tmp_path):
    backend = FakeBackend()
    application = app(tmp_path, backend)
    backend.create_error = BackendFailure(
        "BACKEND_REJECTED", "bad request", status=400, retryable=True
    )
    clear = application.execute("create", agent="gpt", title="One")
    assert clear["code"] == "BACKEND_REJECTED"
    assert len(backend.create_calls) == 1

    backend.create_error = BackendFailure(
        "BACKEND_UNCERTAIN", "lost response", retryable=False
    )
    uncertain = application.execute("create", agent="gpt", title="Two")
    assert uncertain["code"] == "BACKEND_UNCERTAIN"
    assert len(backend.create_calls) == 2
    assert len(audit_rows(application)) == 2


def test_read_returns_complete_excluded_cooking_task(tmp_path):
    task = cooking_task("ref-task", "reference content", "reference")
    backend = FakeBackend({"ref-task": task})
    application = app(tmp_path, backend)

    result = application.execute("read", agent="codex", task_gid="ref-task")

    assert result["ok"] is True
    assert result["data"]["task"] == task
    assert result["task_gid"] == "ref-task"
    assert len(audit_rows(application)) == 1


def test_read_rejects_task_outside_cooking(tmp_path):
    task = cooking_task("other", "")
    task["projects"] = [{"gid": "elsewhere"}]
    task["memberships"] = []
    application = app(tmp_path, FakeBackend({"other": task}))

    result = application.execute("read", agent="claude", task_gid="other")

    assert result["code"] == "UNMANAGED_TASK"
    assert len(audit_rows(application)) == 1


@pytest.mark.parametrize(
    ("kind", "notes", "change_level", "change_reason", "expected_tags", "verification"),
    [
        ("planning", "", None, None, None, None),
        ("initial", PLANNING_NOTE, None, None, ["nutrition-fat"], None),
        (
            "change",
            COMPLETE_NOTE,
            "small",
            "adjust salt",
            ["nutrition-fat"],
            "Verification: original verification text",
        ),
        ("change", COMPLETE_NOTE, "large", "replace method", ["nutrition-fat"], None),
    ],
)
def test_start_accepts_each_valid_starting_shape(
    tmp_path,
    kind,
    notes,
    change_level,
    change_reason,
    expected_tags,
    verification,
):
    backend = FakeBackend({"task": cooking_task("task", notes)})
    application = app(tmp_path, backend)

    result = application.execute(
        "start",
        agent="claude",
        task_gid="task",
        kind=kind,
        change_level=change_level,
        change_reason=change_reason,
    )

    assert result["ok"] is True
    assert result["state"] == "drafting"
    frozen = result["data"]["frozen_release"]
    assert frozen["protocol_release"] == "fixture-v1"
    assert frozen["release_commit"] == "fixture-commit"
    assert frozen["protocol_bundle"]
    assert frozen["canonical_manifest"]["protocol_release"] == "fixture-v1"
    assert frozen["canonical_manifest_text"] == release_fixture().manifest_texts[
        "planning" if kind == "planning" else "complete_task"
    ]
    row = application.conn.execute("SELECT * FROM submissions").fetchone()
    actual_tags = (
        None
        if row["baseline_exemption_tags"] is None
        else json.loads(row["baseline_exemption_tags"])
    )
    assert actual_tags == expected_tags
    assert row["baseline_verification_line"] == verification
    assert len(audit_rows(application)) == 1


@pytest.mark.parametrize(
    ("kind", "notes", "level", "reason", "rule"),
    [
        ("planning", "not empty", None, None, "planning_notes_not_empty"),
        ("planning", "   ", None, None, "planning_notes_not_empty"),
        ("initial", COMPLETE_NOTE, None, None, "missing_heading"),
        ("change", PLANNING_NOTE, "small", "reason", "missing_heading"),
        ("change", COMPLETE_NOTE, None, "reason", "change_level_required"),
        ("change", COMPLETE_NOTE, "small", None, "change_reason_required"),
        ("planning", "", "small", None, "change_arguments_forbidden"),
    ],
)
def test_start_rejects_invalid_shape_or_change_arguments_before_row_creation(
    tmp_path, kind, notes, level, reason, rule
):
    application = app(tmp_path, FakeBackend({"task": cooking_task("task", notes)}))

    result = application.execute(
        "start",
        agent="claude",
        task_gid="task",
        kind=kind,
        change_level=level,
        change_reason=reason,
    )

    assert result["ok"] is False
    assert rule in {error["rule"] for error in result["errors"]}
    assert application.conn.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0
    assert len(audit_rows(application)) == 1


def test_start_rejects_invalid_agent_project_section_and_kind_before_row(tmp_path):
    outside = cooking_task("outside", "")
    outside["projects"] = [{"gid": "other"}]
    outside["memberships"] = []
    tasks = {
        "outside": outside,
        "excluded": cooking_task("excluded", "", "reference"),
        "valid": cooking_task("valid", ""),
    }
    application = app(tmp_path, FakeBackend(tasks))

    cases = [
        dict(agent="other", task_gid="valid", kind="planning"),
        dict(agent="claude", task_gid="outside", kind="planning"),
        dict(agent="claude", task_gid="excluded", kind="planning"),
        dict(agent="claude", task_gid="valid", kind="bogus"),
    ]
    for kwargs in cases:
        result = application.execute(
            "start", change_level=None, change_reason=None, **kwargs
        )
        assert result["ok"] is False

    assert application.conn.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0
    assert len(audit_rows(application)) == len(cases)


def test_competing_starts_create_exactly_one_open_submission(tmp_path):
    db_path = tmp_path / "dish.db"
    initialize_database(db_path).close()
    backend = FakeBackend({"task": cooking_task("task", "")})
    barrier = threading.Barrier(2)
    results = []

    def worker(agent):
        conn = initialize_database(db_path)
        application = DishApplication(conn, backend, release_loader=release_fixture)
        barrier.wait()
        results.append(
            application.execute(
                "start",
                agent=agent,
                task_gid="task",
                kind="planning",
                change_level=None,
                change_reason=None,
            )
        )
        conn.close()

    threads = [
        threading.Thread(target=worker, args=("claude",)),
        threading.Thread(target=worker, args=("gpt",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(result["code"] for result in results) == ["CONFLICT", "OK"]
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM submissions").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 2


def test_inspect_returns_frozen_handoff_routing_markers_and_actions(tmp_path):
    application = app(
        tmp_path, FakeBackend({"task": cooking_task("task", PLANNING_NOTE)})
    )
    started = application.execute(
        "start",
        agent="claude",
        task_gid="task",
        kind="initial",
        change_level=None,
        change_reason=None,
    )
    submission_id = started["submission_id"]

    active = application.execute(
        "inspect", agent="gpt", submission_id=submission_id
    )
    assert active["state"] == "drafting"
    assert active["allowed_actions"] == ["prepare"]
    submission = active["data"]["submission"]
    assert submission["kind"] == "initial"
    assert submission["editor_agent"] == "claude"
    assert submission["frozen_release"]["protocol_bundle"]
    assert submission["completion_markers"] == {
        "research_queue_moved_at": None,
        "notes_written_at": None,
        "destination_moved_at": None,
        "approved_at": None,
        "completed_at": None,
    }
    assert submission["candidate_handoff"] == {
        "stored_by_tool": False,
        "returned_by_inspect": False,
    }
    assert active["data"]["legal_next_actions"] == ["prepare"]

    application.conn.execute(
        "UPDATE submissions SET status = 'consumed', completed_at = 'done' "
        "WHERE submission_id = ?",
        (submission_id,),
    )
    terminal = application.execute(
        "inspect", agent="codex", submission_id=submission_id
    )
    assert terminal["state"] == "consumed"
    assert terminal["allowed_actions"] == []
    assert terminal["data"]["legal_next_actions"] == []
    assert len(audit_rows(application)) == 3


def test_inspect_allowed_actions_cover_every_submission_state(tmp_path):
    application = app(
        tmp_path, FakeBackend({"task": cooking_task("task", "")})
    )
    started = application.execute(
        "start",
        agent="claude",
        task_gid="task",
        kind="planning",
        change_level=None,
        change_reason=None,
    )
    submission_id = started["submission_id"]

    for state in sorted(SUBMISSION_STATES):
        application.conn.execute(
            "UPDATE submissions SET status = ? WHERE submission_id = ?",
            (state, submission_id),
        )
        inspected = application.execute(
            "inspect", agent="gpt", submission_id=submission_id
        )
        assert inspected["state"] == state
        assert inspected["allowed_actions"] == list(ALLOWED_ACTIONS_BY_STATE[state])
        assert inspected["data"]["legal_next_actions"] == list(
            ALLOWED_ACTIONS_BY_STATE[state]
        )


def test_failed_start_audit_keeps_release_and_all_validation_rules(tmp_path):
    application = app(
        tmp_path, FakeBackend({"task": cooking_task("task", "bad notes")})
    )

    result = application.execute(
        "start",
        agent="claude",
        task_gid="task",
        kind="initial",
        change_level=None,
        change_reason=None,
    )

    details = json.loads(audit_rows(application)[0]["details"])
    assert result["code"] == "VALIDATION_FAILED"
    assert details["protocol_release"] == "fixture-v1"
    assert details["release_commit"] == "fixture-commit"
    assert details["submission_kind"] == "initial"
    assert details["errors"] == result["errors"]


def _load_cli_module():
    path = BIN_DIR / "dish"
    loader = SourceFileLoader("dish_cli_stage2", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_cli_argument_failure_is_one_json_result_and_one_audit(tmp_path, capsys):
    application = app(tmp_path, FakeBackend())
    cli = _load_cli_module()

    status = cli.main(["create", "--agent", "claude"], application=application)

    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    payload = json.loads(output[0])
    assert status == 2
    assert payload["code"] == "INVALID_ARGUMENT"
    assert len(audit_rows(application)) == 1
