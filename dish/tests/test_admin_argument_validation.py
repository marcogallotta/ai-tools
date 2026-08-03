from __future__ import annotations

import uuid

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.admin_cli import build_parser
from tests.support.service_scenarios import RUN_ID, post as _post, running as _running
from tests.support.thread_teardown import join_thread, stop_server
from tests.support.submission import _signed


@pytest.mark.parametrize(
    ("command", "required_field"),
    [
        ("migrate", "task_gid"),
        ("reopen-planning", "task_gid"),
        ("reopen", "submission_id"),
        ("recover", "submission_id"),
        ("repair-destination", "submission_id"),
        ("supply-evidence", "submission_id"),
        ("record-human-decision", "submission_id"),
        ("resolved", "submission_id"),
        ("authorize-governed-change", "submission_id"),
        ("discard", "submission_id"),
    ],
)
def test_empty_generic_admin_arguments_are_structured_and_replayable(
    tmp_path, command, required_field
):
    _service, backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    payload = {
        "client": {"run_id": RUN_ID, "request_id": request_id},
        "arguments": {},
    }
    try:
        first_status, first = _post(
            url,
            f"/v1/admin/{command}",
            token="admin-secret",
            payload=payload,
        )
        replay_status, replay = _post(
            url,
            f"/v1/admin/{command}",
            token="admin-secret",
            payload=payload,
        )
    finally:
        stop_server(server, thread)

    assert first_status == replay_status == 200
    assert first["ok"] is False
    assert first["code"] == "INVALID_ARGUMENT"
    assert first["errors"] == [
        {"field": required_field, "rule": "argument_required"}
    ]
    assert first["data"]["request_id"] == request_id
    assert "request_replayed" not in first["data"]

    assert replay["ok"] is False
    assert replay["code"] == "INVALID_ARGUMENT"
    assert replay["errors"] == first["errors"]
    assert replay["data"]["request_id"] == request_id
    assert replay["data"]["request_replayed"] is True
    assert backend.writes == 0


@pytest.mark.parametrize(
    ("arguments", "required_field"),
    [
        ({"submission_id": str(uuid.uuid4()), "reason": "live reread"}, "outcome"),
        ({"submission_id": str(uuid.uuid4()), "outcome": "applied"}, "reason"),
    ],
)
def test_recover_validates_required_fields_before_unknown_operation_and_replays(
    tmp_path, arguments, required_field
):
    _service, backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    payload = {
        "client": {"run_id": RUN_ID, "request_id": request_id},
        "arguments": arguments,
    }
    try:
        first_status, first = _post(
            url,
            "/v1/admin/recover",
            token="admin-secret",
            payload=payload,
        )
        replay_status, replay = _post(
            url,
            "/v1/admin/recover",
            token="admin-secret",
            payload=payload,
        )
    finally:
        stop_server(server, thread)

    assert first_status == replay_status == 200
    assert first["code"] == "INVALID_ARGUMENT"
    assert first["errors"] == [
        {"field": required_field, "rule": "argument_required"}
    ]
    assert replay["code"] == "INVALID_ARGUMENT"
    assert replay["errors"] == first["errors"]
    assert replay["data"]["request_replayed"] is True
    assert backend.writes == 0


@pytest.mark.parametrize(
    ("arguments", "field", "rule"),
    [
        (
            {"submission_id": "terminal", "outcome": " ", "reason": "live reread"},
            "outcome",
            "recovery_outcome_required",
        ),
        (
            {"submission_id": "terminal", "outcome": "applied", "reason": " "},
            "reason",
            "recovery_reason_required",
        ),
    ],
)
def test_recover_validates_blank_fields_before_terminal_operation(
    tmp_path, arguments, field, rule
):

    application, backend, operation_id = _signed(tmp_path)
    submitted = application.execute("submit", submission_id=operation_id)
    assert submitted["ok"]

    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    result = admin.execute(
        "recover",
        **{**arguments, "submission_id": operation_id},
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [{"field": field, "rule": rule}]


def test_record_human_decision_help_discloses_governed_field_boundary(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["record-human-decision", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "does not modify governed fields" in help_text
    assert "authorize-governed-change" in help_text

    with pytest.raises(SystemExit):
        parser.parse_args(["record-human-decision", "-h"])
    detail_help_text = " ".join(capsys.readouterr().out.split())
    assert "does not itself change Exemptions, Locks, or other canonical fields" in detail_help_text


def test_supply_evidence_help_stays_route_specific(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["supply-evidence", "--help"])
    help_text = capsys.readouterr().out
    assert "governed" not in help_text


def test_recover_parser_accepts_generated_inspect_outcome():
    operation_id = str(uuid.uuid4())
    parsed = build_parser().parse_args(
        [
            "recover",
            operation_id,
            "--outcome",
            "inspect",
            "--reason",
            "fresh live reread required",
        ]
    )
    assert parsed.outcome == "inspect"


def test_admin_inspect_is_a_first_class_human_command():
    operation_id = str(uuid.uuid4())
    parsed = build_parser().parse_args(["inspect", operation_id])
    assert parsed.command == "inspect"
    assert parsed.submission_id == operation_id


def test_human_renderer_surfaces_recovery_actions_from_errors():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": False,
        "command": "authorize-governed-change",
        "code": "VALIDATION_FAILED",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": True,
        "allowed_actions": [],
        "data": {"message": "authorization required"},
        "errors": [
            {
                "rule": "governed_change_unauthorized",
                "human_action": {
                    "kind": "authorize-governed-change",
                    "summary": "Authorize the exact Exemptions change.",
                    "effect": "Create one authorization without editing the task.",
                    "shell_command": "dish-admin authorize-governed-change operation-1 --field Exemptions",
                },
            }
        ],
    }
    rendered = render_admin_result(result, profile="prod")
    assert "Could not authorize-governed-change" in rendered
    assert "Authorize the exact Exemptions change." in rendered
    assert "dish-admin authorize-governed-change operation-1" in rendered
    assert '"errors"' not in rendered


def test_human_renderer_explains_authorization_success_without_claiming_a_write():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "authorize-governed-change",
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": False,
        "allowed_actions": [],
        "data": {"field": "Exemptions"},
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Authorization recorded" in rendered
    assert "task itself was not changed" in rendered
    assert "retry the same exact candidate" in rendered
