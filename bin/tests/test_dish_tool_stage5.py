import asyncio
import copy
import json
import threading
from pathlib import Path

import asana
import pytest

from dish_tool import cli
from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database, transition_submission
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import ResolvedRelease

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "protocol-release"
SECTIONS = [
    {"gid": "research", "name": "Research Queue"},
    {"gid": "verification", "name": "Verification Queue"},
    {"gid": "123456", "name": "Planned"},
    {"gid": "other", "name": "Other"},
    {"gid": "sourcing", "name": "Sourcing"},
    {"gid": "reference", "name": "Reference"},
]
PLANNING_NOTE = """# PLANNING BRIEF
Destination section: Planned (123456)
Exemptions: None
"""
CANONICAL_TITLE = "Dish — recognition"
TITLE_ARGS = {
    "dish_name": "Dish",
    "recognition": "recognition",
    "no_role_tags": True,
    "no_blockers": True,
}
COMPLETE_NOTE = """# DISH
Exemptions: None
Destination section: Planned (123456)
Self-verified: yes
Verification: original verification text
## QUANTITIES
Portions: 2
## PROCESS RECORD
"""


def release_fixture():
    planning_text = (FIXTURE_DIR / "dish-planning-manifest.json").read_text()
    complete_text = (FIXTURE_DIR / "dish-complete-task-manifest.json").read_text()
    return ResolvedRelease(
        version="fixture-v2-structured-title",
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
        manifest_texts={"planning": planning_text, "complete_task": complete_text},
    )


def task(notes=PLANNING_NOTE, section="verification"):
    name = next(item["name"] for item in SECTIONS if item["gid"] == section)
    return {
        "gid": "task",
        "name": CANONICAL_TITLE,
        "notes": notes,
        "projects": [{"gid": "1215089183018968"}],
        "memberships": [
            {
                "project": {"gid": "1215089183018968"},
                "section": {"gid": section, "name": name},
            }
        ],
    }


class Backend:
    def __init__(self, *, section="verification"):
        self.item = task(section=section)
        self.sections = list(SECTIONS)
        self.notes_calls = []
        self.moves = []
        self.notes_error = None
        self.move_error = None
        self.on_notes_success = None
        self.notes_entered = None
        self.notes_release = None

    def list_sections(self, project_gid):
        return copy.deepcopy(self.sections)

    def read_task(self, task_gid):
        return copy.deepcopy(self.item)

    def create_bare_task(self, **kwargs):
        raise AssertionError

    def update_task_content(self, *, task_gid, title, notes):
        self.notes_calls.append((task_gid, title, notes))
        if self.notes_entered is not None:
            self.notes_entered.set()
        if self.notes_release is not None:
            assert self.notes_release.wait(timeout=5)
        if self.notes_error is not None:
            raise self.notes_error
        self.item["name"] = title
        self.item["notes"] = notes
        if self.on_notes_success is not None:
            self.on_notes_success()

    def move_task_to_section(self, *, task_gid, section_gid):
        self.moves.append((task_gid, section_gid))
        if self.move_error is not None:
            raise self.move_error
        name = next(item["name"] for item in self.sections if item["gid"] == section_gid)
        self.item["memberships"][0]["section"] = {"gid": section_gid, "name": name}


def make_app(tmp_path, backend=None, db_name="dish.db"):
    backend = backend or Backend()
    conn = initialize_database(tmp_path / db_name)
    return DishApplication(conn, backend, release_loader=release_fixture), backend


def candidate(tmp_path, text=COMPLETE_NOTE, name="final.md"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def ready_submission(app, tmp_path, *, kind="initial"):
    if kind == "planning":
        app.backend.item = task(notes="", section="research")
    started = app.execute("start", agent="claude", task_gid="task", kind=kind)
    assert started["ok"]
    sid = started["submission_id"]
    prepare_kwargs = {
        "agent": "claude",
        "submission_id": sid,
        "file_path": candidate(
            tmp_path,
            PLANNING_NOTE if kind == "planning" else COMPLETE_NOTE,
            f"{kind}.md",
        ),
    }
    if kind != "planning":
        prepare_kwargs.update(TITLE_ARGS)
    prepared = app.execute("prepare", **prepare_kwargs)
    if prepared["state"] == "awaiting_verification":
        approved = app.execute(
            "approve",
            agent="gpt",
            submission_id=sid,
            file_path=candidate(tmp_path, COMPLETE_NOTE, "approved.md"),
            correction="none",
        )
        assert approved["state"] == "ready"
    else:
        assert prepared["state"] == "ready"
    return sid


def saved(app, sid):
    return app.conn.execute(
        "SELECT * FROM submissions WHERE submission_id = ?", (sid,)
    ).fetchone()


def submit_audit(app):
    row = app.conn.execute(
        "SELECT actor_agent, details FROM audit_events "
        "WHERE event_type = 'dish.submit' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["actor_agent"], json.loads(row["details"])


def test_prewrite_file_failure_never_reaches_backend(tmp_path):
    app, backend = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)

    result = app.execute("submit", submission_id=sid, file_path="/missing/final.md")

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["state"] == "ready"
    assert backend.notes_calls == []
    row = saved(app, sid)
    assert row["write_attempt_id"] is None
    assert row["status"] == "ready"


def test_confirmed_rejection_returns_to_ready_and_clears_attempt(tmp_path):
    app, backend = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)
    backend.notes_error = BackendFailure(
        "BACKEND_REJECTED", "explicit rejection", status=400, retryable=True
    )

    result = app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))

    assert result["code"] == "BACKEND_REJECTED"
    assert result["state"] == "ready"
    assert len(backend.notes_calls) == 1
    row = saved(app, sid)
    assert row["write_attempt_id"] is None
    assert row["in_flight_at"] is None
    actor, details = submit_audit(app)
    assert actor == "claude"
    assert details["write_outcome"] == "confirmed_non_application"
    assert details["state"] == "ready"


def test_uncertain_write_keeps_attempt_and_requires_recovery(tmp_path):
    app, backend = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)
    backend.notes_error = BackendFailure(
        "BACKEND_UNCERTAIN", "lost response", retryable=False
    )

    result = app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))

    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["state"] == "uncertain"
    row = saved(app, sid)
    assert row["write_attempt_id"]
    assert row["in_flight_at"]
    actor, details = submit_audit(app)
    assert actor == "claude"
    assert details["write_outcome"] == "uncertain"


def test_success_writes_once_moves_from_verification_and_consumes(tmp_path):
    app, backend = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)
    final_path = candidate(tmp_path, COMPLETE_NOTE + "final\n")

    result = app.execute("submit", submission_id=sid, file_path=final_path)

    assert result["state"] == "consumed"
    assert backend.notes_calls == [("task", CANONICAL_TITLE, COMPLETE_NOTE + "final\n")]
    assert backend.moves == [("task", "123456")]
    row = saved(app, sid)
    assert row["task_content_written_at"]
    assert row["destination_moved_at"]
    assert row["completed_at"]
    actor, details = submit_audit(app)
    assert actor == "claude"
    assert details["write_outcome"] == "confirmed_success"
    assert details["handoff"] == "moved_to_destination"


def test_written_retry_ignores_file_and_never_repeats_notes(tmp_path):
    app, backend = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)
    transition_submission(
        app.conn,
        sid,
        {"ready"},
        "written",
        updates={"task_content_written_at": "already-written"},
    )

    result = app.execute("submit", submission_id=sid, file_path="/gone/final.md")

    assert result["state"] == "consumed"
    assert backend.notes_calls == []
    assert backend.moves == [("task", "123456")]


@pytest.mark.parametrize(
    ("section", "kind", "expected_handoff"),
    [
        ("123456", "initial", "already_at_destination"),
        ("research", "planning", "planning_research_queue"),
        ("other", "initial", "manual_override_preserved"),
    ],
)
def test_no_move_handoff_cases_consume_without_mutation(
    tmp_path, section, kind, expected_handoff
):
    backend = Backend(section=section)
    app, backend = make_app(tmp_path, backend)
    sid = ready_submission(app, tmp_path, kind=kind)
    if section != "research":
        backend.item = task(notes=PLANNING_NOTE, section=section)

    result = app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))

    assert result["state"] == "consumed"
    assert len(backend.notes_calls) == 1
    assert backend.moves == []
    _, details = submit_audit(app)
    assert details["handoff"] == expected_handoff


def test_move_failure_stays_written_and_retry_is_move_only(tmp_path):
    app, backend = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)
    backend.move_error = BackendFailure(
        "BACKEND_REJECTED", "move rejected", status=400, retryable=True
    )

    first = app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))

    assert first["code"] == "BACKEND_REJECTED"
    assert first["state"] == "written"
    assert len(backend.notes_calls) == 1
    assert len(backend.moves) == 1
    backend.move_error = None

    second = app.execute("submit", submission_id=sid, file_path="/gone/final.md")

    assert second["state"] == "consumed"
    assert len(backend.notes_calls) == 1
    assert len(backend.moves) == 2


def test_simultaneous_ready_submits_allow_one_write_attempt(tmp_path):
    db_path = tmp_path / "race.db"
    backend = Backend()
    setup = DishApplication(
        initialize_database(db_path), backend, release_loader=release_fixture
    )
    sid = ready_submission(setup, tmp_path)
    setup.conn.close()
    backend.notes_entered = threading.Event()
    backend.notes_release = threading.Event()
    results = []

    def first():
        app = DishApplication(
            initialize_database(db_path), backend, release_loader=release_fixture
        )
        results.append(
            app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))
        )
        app.conn.close()

    thread = threading.Thread(target=first)
    thread.start()
    assert backend.notes_entered.wait(timeout=5)
    competing = DishApplication(
        initialize_database(db_path), backend, release_loader=release_fixture
    )
    results.append(
        competing.execute("submit", submission_id=sid, file_path=candidate(tmp_path))
    )
    competing.conn.close()
    backend.notes_release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert sorted(result["code"] for result in results) == ["OK", "WRONG_STATE"]
    assert len(backend.notes_calls) == 1


def test_stale_attempt_cannot_commit_success_after_recovery(tmp_path):
    db_path = tmp_path / "stale.db"
    app, backend = make_app(tmp_path, db_name="stale.db")
    sid = ready_submission(app, tmp_path)

    def invalidate_attempt():
        conn = initialize_database(db_path)
        conn.execute(
            "UPDATE submissions SET status='ready', write_attempt_id=NULL, "
            "in_flight_at=NULL, in_flight_hostname=NULL, in_flight_pid=NULL, "
            "in_flight_process_start=NULL WHERE submission_id=?",
            (sid,),
        )
        conn.close()

    backend.on_notes_success = invalidate_attempt
    result = app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))

    assert result["code"] == "CONFLICT"
    assert saved(app, sid)["status"] == "ready"
    assert backend.moves == []


def test_consumed_submission_cannot_be_reused(tmp_path):
    app, backend = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)
    first = app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))
    assert first["state"] == "consumed"
    notes_count = len(backend.notes_calls)
    move_count = len(backend.moves)

    second = app.execute("submit", submission_id=sid, file_path=candidate(tmp_path))

    assert second["code"] == "WRONG_STATE"
    assert second["state"] == "consumed"
    assert len(backend.notes_calls) == notes_count
    assert len(backend.moves) == move_count


def test_submit_argument_failure_uses_recorded_editor_and_state(tmp_path, capsys):
    app, _ = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)

    status = cli.main(["submit", sid], application=app)

    payload = json.loads(capsys.readouterr().out)
    assert status == 2
    assert payload["code"] == "INVALID_ARGUMENT"
    assert payload["state"] == "ready"
    actor, details = submit_audit(app)
    assert actor == "claude"
    assert details["state"] == "ready"


def test_submit_cli_takes_no_agent_and_audits_recorded_editor(tmp_path, capsys):
    app, _ = make_app(tmp_path)
    sid = ready_submission(app, tmp_path)

    status = cli.main(
        ["submit", sid, "--file", candidate(tmp_path)], application=app
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["state"] == "consumed"
    actor, _ = submit_audit(app)
    assert actor == "claude"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("response lost"), "BACKEND_UNCERTAIN"),
        (ConnectionResetError("reset"), "BACKEND_UNCERTAIN"),
        (asyncio.CancelledError(), "BACKEND_UNCERTAIN"),
    ],
)
def test_asana_notes_transport_failures_are_uncertain(monkeypatch, failure, expected):
    class TasksApi:
        def __init__(self, client):
            pass

        def update_task(self, body, task_gid, opts, **kwargs):
            raise failure

    monkeypatch.setattr(asana, "TasksApi", TasksApi)
    with pytest.raises(BackendFailure) as exc:
        AsanaBackend(api_client=object()).update_task_content(
            task_gid="task", title=CANONICAL_TITLE, notes="notes"
        )
    assert exc.value.code == expected


def test_asana_notes_explicit_rejection_server_error_and_malformed_response(
    monkeypatch,
):
    class Rejection(Exception):
        status = 400
        body = "bad request"

    class ServerFailure(Exception):
        status = 503
        body = "unavailable"

    outcomes = [
        Rejection(),
        ServerFailure(),
        {"wrong": "envelope"},
        {"data": "not-a-task"},
        {"data": {"gid": "different-task"}},
        {"data": {"gid": "task"}},
    ]

    class TasksApi:
        def __init__(self, client):
            pass

        def update_task(self, body, task_gid, opts, **kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(asana, "TasksApi", TasksApi)
    backend = AsanaBackend(api_client=object())

    with pytest.raises(BackendFailure) as rejected:
        backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="one")
    assert rejected.value.code == "BACKEND_REJECTED"

    with pytest.raises(BackendFailure) as uncertain_5xx:
        backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="two")
    assert uncertain_5xx.value.code == "BACKEND_UNCERTAIN"

    with pytest.raises(BackendFailure) as malformed_envelope:
        backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="three")
    assert malformed_envelope.value.code == "BACKEND_UNCERTAIN"

    with pytest.raises(BackendFailure) as malformed_data:
        backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="four")
    assert malformed_data.value.code == "BACKEND_UNCERTAIN"

    with pytest.raises(BackendFailure) as wrong_task:
        backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="five")
    assert wrong_task.value.code == "BACKEND_UNCERTAIN"

    backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="six")


def test_asana_notes_setup_failure_is_confirmed_pre_send(monkeypatch):
    backend = AsanaBackend(api_client=object())

    def fail_client():
        raise DishRuleError(
            "INTERNAL_ERROR", "local configuration failed", rule="local_setup"
        )

    monkeypatch.setattr(backend, "client", fail_client)
    with pytest.raises(BackendFailure) as exc:
        backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="notes")

    assert exc.value.code == "BACKEND_REJECTED"
    assert exc.value.phase == "pre_send"


def test_asana_notes_cancelled_setup_is_confirmed_pre_send(monkeypatch):
    backend = AsanaBackend(api_client=object())

    def cancel_client():
        raise asyncio.CancelledError()

    monkeypatch.setattr(backend, "client", cancel_client)
    with pytest.raises(BackendFailure) as exc:
        backend.update_task_content(task_gid="task", title=CANONICAL_TITLE, notes="notes")

    assert exc.value.code == "BACKEND_REJECTED"
    assert exc.value.phase == "pre_send"
