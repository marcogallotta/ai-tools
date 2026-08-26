from __future__ import annotations

import builtins

from dish_service import admin_cli
from dish_tool import constants
from dish_tool.admin import DishAdminApplication
from dish_tool.database import confirm_task_content, create_operation
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import BackendFailure
from dish_tool.identifiers import stable_dish_uuid_for_asana_identity
from dish_tool.models import OperationActors
from tests.support.asana_backend import StatefulAsanaBackend


TASK_GID = "1217843230325135"
HISTORY_GID = "1217000000000000"


def _application(monkeypatch, *, completed: bool = False):
    monkeypatch.setattr(constants, "COOKING_HISTORY_PROJECT_GID", HISTORY_GID)
    conn = initialize_database(":memory:")
    backend = StatefulAsanaBackend(
        task_gid=TASK_GID,
        title="Mapo tofu",
        notes="Canonical notes stay byte-for-byte unchanged.",
        completed=completed,
    )
    confirm_task_content(
        conn,
        task_gid=TASK_GID,
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
        boundary="test",
    )
    app = DishAdminApplication(
        conn,
        backend=backend,
        invocation_request_id="11111111-1111-4111-8111-111111111111",
        invocation_run_id="22222222-2222-4222-8222-222222222222",
    )
    return conn, backend, app


def test_archive_requires_confirmation_without_mutating(monkeypatch) -> None:
    _conn, backend, app = _application(monkeypatch)
    dish_id = stable_dish_uuid_for_asana_identity("task", TASK_GID)

    result = app.execute("archive", dish=TASK_GID, confirmed=False)

    assert result["code"] == "CONFIRMATION_REQUIRED"
    assert result["data"]["confirmation_prompt"] == (
        f"Archive “Mapo tofu” ({dish_id})? It will leave active/search views; "
        "all history will be preserved. [y/N]"
    )
    assert backend.writes == 0
    assert backend.completed is False
    assert backend.tasks[TASK_GID]["project_gids"] == {constants.COOKING_PROJECT_GID}


def test_archive_cli_has_no_reason_argument() -> None:
    parser = admin_cli.build_parser()

    try:
        parser.parse_args(["archive", TASK_GID, "--reason", "unused"])
    except Exception as exc:
        assert getattr(exc, "code", None) == "INVALID_ARGUMENT"
    else:
        raise AssertionError("archive unexpectedly accepted --reason")


def test_archive_cli_empty_confirmation_cancels_successfully(
    monkeypatch, capsys
) -> None:
    _conn, backend, app = _application(monkeypatch)
    prompts: list[str] = []

    def decline(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(builtins, "input", decline)
    monkeypatch.setattr(admin_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(admin_cli.sys.stdout, "isatty", lambda: True)

    status = admin_cli.main(["archive", TASK_GID], application=app)

    assert status == 0
    assert prompts == [
        app.execute("archive", dish=TASK_GID, confirmed=False)["data"][
            "confirmation_prompt"
        ]
    ]
    assert "no Dish was changed" in capsys.readouterr().out
    assert backend.writes == 0


def test_archive_orders_effects_preserves_identity_and_records_provenance(monkeypatch) -> None:
    conn, backend, app = _application(monkeypatch)
    original = (backend.title, backend.notes)

    result = app.execute("archive", dish=TASK_GID, confirmed=True)

    assert result["ok"] is True
    assert result["data"]["already_archived"] is False
    assert [
        call.operation
        for call in backend.calls()
        if call.operation in {
            "update_task_completed",
            "add_task_to_project",
            "remove_task_from_project",
        }
    ] == [
        "update_task_completed",
        "add_task_to_project",
        "remove_task_from_project",
    ]
    assert (backend.title, backend.notes) == original
    assert backend.completed is True
    assert backend.tasks[TASK_GID]["project_gids"] == {HISTORY_GID}
    active_rows, _cursor = backend.list_tasks_for_section("rq")
    assert active_rows == []
    audit = conn.execute(
        """SELECT details FROM audit_events
             WHERE event_type='dish-admin.archive' AND result_ok=1"""
    ).fetchone()
    assert audit is not None
    assert '"system_reason":"admin_archive"' in audit["details"]
    assert '"authority_mode":"asana"' in audit["details"]
    assert '"request_id":"11111111-1111-4111-8111-111111111111"' in audit["details"]


def test_archive_new_invocation_is_idempotent_only_with_dish_audit_evidence(monkeypatch) -> None:
    _conn, backend, app = _application(monkeypatch)
    assert app.execute("archive", dish=TASK_GID, confirmed=True)["ok"] is True
    effect_count = backend.writes

    replay = DishAdminApplication(
        app.conn,
        backend=backend,
        invocation_request_id="33333333-3333-4333-8333-333333333333",
    ).execute("archive", dish=TASK_GID, confirmed=True)
    inspected = DishAdminApplication(app.conn, backend=backend).execute(
        "inspect", dish=TASK_GID
    )

    assert replay["ok"] is True
    assert replay["data"]["already_archived"] is True
    assert backend.writes == effect_count
    assert inspected["state"] == "archived"
    assert inspected["data"]["completion_state"] == "archived"


def test_manual_completion_is_not_relabelled_as_archived(monkeypatch) -> None:
    _conn, backend, app = _application(monkeypatch, completed=True)

    result = app.execute("archive", dish=TASK_GID, confirmed=True)

    assert result["code"] == "TASK_NOT_ACTIVE"
    assert backend.writes == 0


def test_archive_refuses_an_open_operation_before_any_effect(monkeypatch) -> None:
    conn, backend, app = _application(monkeypatch)
    head = conn.execute(
        "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid=?",
        (TASK_GID,),
    ).fetchone()
    create_operation(
        conn,
        task_gid=TASK_GID,
        operation_kind="planning",
        expected_identity=head["last_confirmed_identity"],
        schema_version="2",
        expected_section_gid="rq",
        actors=OperationActors(editor_agent="gpt", run_id="run"),
    )

    result = app.execute("archive", dish=TASK_GID, confirmed=True)

    assert result["code"] == "TASK_NOT_RESTING"
    assert result["errors"][0].get("open_operation_id")
    assert backend.writes == 0


def test_archive_fails_closed_when_history_project_is_not_distinct(monkeypatch) -> None:
    _conn, backend, app = _application(monkeypatch)
    monkeypatch.setattr(
        constants, "COOKING_HISTORY_PROJECT_GID", constants.COOKING_PROJECT_GID
    )

    result = app.execute("archive", dish=TASK_GID, confirmed=True)

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "cooking_history_project_invalid"
    assert backend.writes == 0


def test_archive_ambiguous_suffix_succeeds_only_when_final_facts_are_observed(
    monkeypatch,
) -> None:
    _conn, backend, app = _application(monkeypatch)

    def fail_after_remove(**_kwargs) -> None:
        raise BackendFailure(
            "BACKEND_UNCERTAIN",
            "response lost",
            retryable=False,
        )

    backend.after("remove_task_from_project", fail_after_remove)

    result = app.execute("archive", dish=TASK_GID, confirmed=True)

    assert result["ok"] is True
    assert backend.tasks[TASK_GID]["project_gids"] == {HISTORY_GID}


def test_archive_does_not_blindly_retry_a_partial_ambiguous_effect(monkeypatch) -> None:
    _conn, backend, app = _application(monkeypatch)

    def fail_after_add(**_kwargs) -> None:
        raise BackendFailure(
            "BACKEND_UNCERTAIN",
            "response lost",
            retryable=False,
        )

    backend.after("add_task_to_project", fail_after_add)

    result = app.execute("archive", dish=TASK_GID, confirmed=True)

    assert result["code"] == "BACKEND_UNCERTAIN"
    assert len(backend.calls("add_task_to_project")) == 1
    assert len(backend.calls("remove_task_from_project")) == 0
    assert backend.tasks[TASK_GID]["project_gids"] == {
        constants.COOKING_PROJECT_GID,
        HISTORY_GID,
    }
