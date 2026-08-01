from __future__ import annotations

import json
import uuid

import pytest

from dish_tool.database import initialize_database
from tests.support.service_scenarios import RUN_ID, post as _post, running as _running
from tests.support.thread_teardown import join_thread, stop_server


UNKNOWN_OPERATION = "99999999-9999-4999-8999-999999999999"


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("path", "token", "body", "command"),
    [
        (
            "/v1/action/submit",
            "action-secret",
            {"arguments": {"submission_id": UNKNOWN_OPERATION}},
            "submit",
        ),
        (
            "/v1/admin/recover",
            "admin-secret",
            {
                "arguments": {
                    "submission_id": UNKNOWN_OPERATION,
                    "outcome": "applied",
                    "reason": "unknown-operation replay probe",
                }
            },
            "recover",
        ),
        (
            "/v1/action/renew-lease",
            "action-secret",
            {"arguments": {"operation_id": UNKNOWN_OPERATION}},
            "renew-lease",
        ),
        (
            f"/v1/admin/leases/{UNKNOWN_OPERATION}/recover",
            "admin-secret",
            {"reason": "operator recovery"},
            "recover-lease",
        ),
    ],
)
def test_unknown_operation_results_are_completed_and_replayable(
    tmp_path, path, token, body, command
):
    service, _backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    payload = {
        **body,
        "client": {"run_id": RUN_ID, "request_id": request_id},
    }
    try:
        first_status, first = _post(url, path, token=token, payload=payload)
        replay_status, replay = _post(url, path, token=token, payload=payload)
    finally:
        stop_server(server, thread)

    assert first_status == replay_status == 200
    assert first["ok"] is False
    assert first["command"] == command
    assert first["code"] == "NOT_FOUND"
    assert first["submission_id"] == UNKNOWN_OPERATION
    assert first["errors"] == [{"rule": "operation_not_found"}]
    assert first["data"]["request_id"] == request_id

    assert replay["code"] == "NOT_FOUND"
    assert replay["submission_id"] == UNKNOWN_OPERATION
    assert replay["errors"] == first["errors"]
    assert replay["data"]["request_replayed"] is True

    conn = initialize_database(service.config.db_path)
    try:
        row = conn.execute(
            "SELECT status, operation_id, result_json FROM service_requests "
            "WHERE request_id=?",
            (request_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "completed"
    assert row["operation_id"] is None
    assert json.loads(row["result_json"])["submission_id"] == UNKNOWN_OPERATION


@pytest.mark.smoke
def test_unknown_operation_request_id_still_rejects_changed_reuse(tmp_path):
    _service, _backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    first_payload = {
        "client": {"run_id": RUN_ID, "request_id": request_id},
        "arguments": {"submission_id": UNKNOWN_OPERATION},
    }
    changed_payload = {
        "client": {"run_id": RUN_ID, "request_id": request_id},
        "arguments": {
            "submission_id": "88888888-8888-4888-8888-888888888888"
        },
    }
    try:
        first_status, first = _post(
            url,
            "/v1/action/submit",
            token="action-secret",
            payload=first_payload,
        )
        conflict_status, conflict = _post(
            url,
            "/v1/action/submit",
            token="action-secret",
            payload=changed_payload,
        )
    finally:
        stop_server(server, thread)

    assert first_status == conflict_status == 200
    assert first["code"] == "NOT_FOUND"
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"] == [
        {"request_id": request_id, "rule": "service_request_identity_conflict"}
    ]
