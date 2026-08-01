from __future__ import annotations

import threading

from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from dish_tool.database import initialize_database
from tests._service_test_helpers import (
    complete_service_submission as _complete_service_submission,
    post as _post,
    service as _service,
)
from tests.support.thread_teardown import join_thread, start_server_thread, stop_server


EDITOR_RUN_ID = "99999999-9999-4999-8999-999999999999"
SUBMIT_REQUEST_ID = "90000000-0000-4000-8000-000000000001"
START_REQUEST_ID = "90000000-0000-4000-8000-000000000002"
INVALID_PREPARE_REQUEST_ID = "90000000-0000-4000-8000-000000000003"


def _effect_counts(service):
    conn = initialize_database(service.config.db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "operation_steps",
                "write_attempts",
                "movement_attempts",
                "content_versions",
                "verification_cycles",
            )
        }
    finally:
        conn.close()


def _post_invalid_model_twice(service, started, backend):
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    payload = {
        "client": {"run_id": EDITOR_RUN_ID, "request_id": INVALID_PREPARE_REQUEST_ID},
        "arguments": {
            "submission_id": started["submission_id"],
            "agent": "gpt",
            "model": "bad\nmodel",
            "file_text": backend.title + "\n" + backend.notes,
        },
    }
    try:
        first = _post(
            f"http://{host}:{port}",
            "/v1/action/prepare",
            token="action-secret",
            payload=payload,
        )
        replay = _post(
            f"http://{host}:{port}",
            "/v1/action/prepare",
            token="action-secret",
            payload=payload,
        )
        return first, replay
    finally:
        stop_server(server, thread)


def _assert_invalid_model_replay(first_response, replay_response):
    first_status, first = first_response
    replay_status, replay = replay_response
    assert first_status == replay_status == 200
    assert first["code"] == "INVALID_ARGUMENT"
    assert first["retryable"] is True
    assert first["errors"] == [
        {"rule": "model_invalid_characters", "field": "model"}
    ]
    assert first["data"]["request_id"] == INVALID_PREPARE_REQUEST_ID
    assert replay["errors"] == first["errors"]
    assert replay["data"]["request_replayed"] is True
    assert replay["data"]["request_id"] == INVALID_PREPARE_REQUEST_ID


def _assert_no_prepare_effects(service, backend, before_task, before_effects, before_counts):
    assert (backend.title, backend.notes, backend.section) == before_task
    assert (backend.writes, backend.moves) == before_effects
    after_counts = _effect_counts(service)
    conn = initialize_database(service.config.db_path)
    try:
        request = conn.execute(
            "SELECT status,result_json FROM service_requests WHERE request_id=?",
            (INVALID_PREPARE_REQUEST_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert after_counts == before_counts
    assert request["status"] == "completed"
    assert "model_invalid_characters" in request["result_json"]


def test_action_rejects_control_character_model_before_noop_prepare_effects(tmp_path):
    service, backend = _service(tmp_path)
    verifier, operation_id = _complete_service_submission(service, backend)
    submitted = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=SUBMIT_REQUEST_ID,
    )
    assert submitted["ok"], submitted

    editor = ServicePrincipal(owner_id="action", run_id=EDITOR_RUN_ID)
    started = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "change",
            "change_level": "small",
            "change_reason": "confirm no-op candidate handling",
        },
        principal=editor,
        request_id=START_REQUEST_ID,
    )
    assert started["ok"], started

    before_task = (backend.title, backend.notes, backend.section)
    before_effects = (backend.writes, backend.moves)
    before_counts = _effect_counts(service)
    first, replay = _post_invalid_model_twice(service, started, backend)
    _assert_invalid_model_replay(first, replay)
    _assert_no_prepare_effects(
        service, backend, before_task, before_effects, before_counts
    )
