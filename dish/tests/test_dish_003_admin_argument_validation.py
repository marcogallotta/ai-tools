from __future__ import annotations

import uuid

import pytest

from tests.test_dish_tool_r54_hard_request_identity import RUN_ID, _post, _running


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
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

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
