import json
import sqlite3
from pathlib import Path

import pytest

from dish_tool import admin_cli, cli
from dish_tool.admin import DishAdminApplication
from dish_tool.commands import DishApplication
from dish_tool.constants import UNSUPPORTED_WORKFLOW_STATE
from dish_tool.database import initialize_database
from dish_tool.models import ResolvedRelease

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
RELEASE_ROOT = FIXTURE_ROOT / "protocol-release"
LEGACY_TASK_PATH = (
    FIXTURE_ROOT / "compatibility-freeze" / "legacy-ready-task.json"
)


class RecordingBackend:
    def __init__(self, task=None):
        self.task = task
        self.calls = []

    def __getattr__(self, name):
        def recorded(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "read_task" and self.task is not None:
                return dict(self.task)
            raise AssertionError(f"backend call was not expected: {name}")

        return recorded


def release_fixture() -> ResolvedRelease:
    planning_text = (RELEASE_ROOT / "dish-planning-manifest.json").read_text()
    complete_text = (RELEASE_ROOT / "dish-complete-task-manifest.json").read_text()
    return ResolvedRelease(
        version="task-pinned-release-v1a",
        commit="fixture-commit",
        root=RELEASE_ROOT,
        protocols={
            "planning": (RELEASE_ROOT / "dish-planning-protocol.md").read_text(),
            "research": (RELEASE_ROOT / "dish-research-protocol.md").read_text(),
            "verification": (
                RELEASE_ROOT / "dish-verification-protocol.md"
            ).read_text(),
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


def dish_application(db_path, backend):
    return DishApplication(
        initialize_database(db_path),
        backend,
        release_loader=release_fixture,
    )


def audit_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT event_type, details FROM audit_events ORDER BY rowid"
        ).fetchall()


def assert_incompatible(payload):
    assert payload["ok"] is False
    assert payload["code"] == "PROTOCOL_INCOMPATIBLE"
    assert payload["state"] == UNSUPPORTED_WORKFLOW_STATE
    assert payload["retryable"] is False
    assert payload["allowed_actions"] == []
    assert payload["data"]["compatibility"]["status"] == "unsupported"
    assert payload["data"]["compatibility"]["diagnostic_read_only"] is False


def test_legacy_ready_fixture_is_available_only_as_diagnostic_read(
    tmp_path, monkeypatch, capsys
):
    task = json.loads(LEGACY_TASK_PATH.read_text())
    backend = RecordingBackend(task)
    db_path = tmp_path / "dish.db"
    app = dish_application(db_path, backend)
    monkeypatch.setattr(cli, "build_application", lambda: app)

    status = cli.main(["read", task["gid"], "--agent", "claude"])

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["ok"] is True
    assert payload["code"] == "OK"
    assert payload["state"] == UNSUPPORTED_WORKFLOW_STATE
    assert payload["retryable"] is False
    assert payload["allowed_actions"] == []
    assert payload["data"]["task"] == task
    assert payload["data"]["compatibility"] == {
        "status": "unsupported",
        "workflow": "task-pinned-release-v1a",
        "diagnostic_read_only": True,
        "message": (
            "the installed dish workflow is a legacy task-pinned release "
            "implementation and is not compatible with the current dish "
            "protocol/schema baseline"
        ),
    }
    assert backend.calls == [("read_task", (task["gid"],), {})]
    rows = audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "dish.read"


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "--agent", "claude", "--title", "New dish"],
        ["inspect", "submission", "--agent", "claude"],
        ["start", "task", "--agent", "claude", "--kind", "planning"],
        [
            "prepare",
            "submission",
            "--agent",
            "claude",
            "--file",
            "candidate.md",
        ],
        [
            "approve",
            "submission",
            "--agent",
            "gpt",
            "--file",
            "candidate.md",
            "--correction",
            "none",
        ],
        [
            "reject",
            "submission",
            "--agent",
            "gpt",
            "--reason",
            "large correction",
        ],
        ["submit", "submission", "--file", "candidate.md"],
    ],
)
def test_dish_workflow_commands_fail_before_backend_or_submission_mutation(
    tmp_path, monkeypatch, capsys, argv
):
    backend = RecordingBackend()
    db_path = tmp_path / "dish.db"
    app = dish_application(db_path, backend)
    monkeypatch.setattr(cli, "build_application", lambda: app)

    status = cli.main(argv)

    payload = json.loads(capsys.readouterr().out)
    assert status == 3
    assert_incompatible(payload)
    assert backend.calls == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0
    rows = audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["event_type"] == f"dish.{argv[0]}"
    assert json.loads(rows[0]["details"])["code"] == "PROTOCOL_INCOMPATIBLE"


def insert_ready_submission(conn, submission_id="legacy-ready"):
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, status, created_at
        ) VALUES (?, 'task-ready', 'initial', 'task-pinned-release-v1a', 'legacy-commit',
                  '{}', '{}', 'claude', 'claude', 'ready',
                  '2026-07-21T00:00:00Z')
        """,
        (submission_id,),
    )
    return submission_id


def test_malformed_blocked_command_cannot_expose_legacy_ready_action(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "dish.db"
    app = dish_application(db_path, RecordingBackend())
    submission_id = insert_ready_submission(app.conn)
    monkeypatch.setattr(cli, "build_application", lambda: app)

    status = cli.main(["submit", submission_id])

    payload = json.loads(capsys.readouterr().out)
    assert status == 3
    assert_incompatible(payload)
    assert payload["data"]["legacy_state"] == "ready"
    assert len(audit_rows(db_path)) == 1


@pytest.mark.parametrize(
    "argv",
    [
        [
            "recover",
            "legacy-ready",
            "--outcome",
            "applied",
            "--reason",
            "inspected",
        ],
        ["discard", "legacy-ready", "--reason", "retired workflow"],
        ["unblock", "legacy-ready", "--reason", "new evidence"],
    ],
)
def test_admin_commands_fail_before_local_workflow_mutation(
    tmp_path, monkeypatch, capsys, argv
):
    db_path = tmp_path / "dish.db"
    app = DishAdminApplication(initialize_database(db_path))
    insert_ready_submission(app.conn)
    monkeypatch.setattr(admin_cli, "build_application", lambda: app)

    status = admin_cli.main(argv)

    payload = json.loads(capsys.readouterr().out)
    assert status == 3
    assert_incompatible(payload)
    assert payload["data"]["legacy_state"] == "ready"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT status FROM submissions WHERE submission_id='legacy-ready'"
        ).fetchone()[0] == "ready"
    rows = audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["event_type"] == f"dish-admin.{argv[0]}"
    assert json.loads(rows[0]["details"])["code"] == "PROTOCOL_INCOMPATIBLE"
