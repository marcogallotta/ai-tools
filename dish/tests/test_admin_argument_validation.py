from __future__ import annotations

import uuid

import pytest

from dish_tool.admin import DishAdminApplication
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
