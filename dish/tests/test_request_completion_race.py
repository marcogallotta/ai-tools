from __future__ import annotations

import uuid

from dish_service.request_replay import begin_request, complete_request
from dish_tool.database import initialize_database
from dish_tool.results import result_envelope, error_envelope
from dish_tool.errors import DishRuleError


def _pending(conn, request_id: str) -> None:
    row, started = begin_request(
        conn,
        request_id=request_id,
        owner_id="owner",
        run_id="11111111-1111-4111-8111-111111111111",
        command="prepare",
        arguments={"submission_id": "missing"},
    )
    assert started is True
    assert row["status"] == "pending"


def test_original_executor_returns_stored_uncertainty_when_recovery_wins(tmp_path):
    db_path = tmp_path / "dish.db"
    original = initialize_database(db_path)
    recovery = initialize_database(db_path)
    request_id = str(uuid.uuid4())
    try:
        _pending(original, request_id)
        uncertain = error_envelope(
            "prepare",
            DishRuleError(
                "BACKEND_UNCERTAIN",
                "durable completion requires inspection",
                rule="service_request_result_missing",
                retryable=False,
            ),
        )
        uncertain.setdefault("data", {})["request_id"] = request_id
        complete_request(recovery, request_id=request_id, result=uncertain)

        local_success = result_envelope(command="prepare", data={"message": "done"})
        local_success.setdefault("data", {})["request_id"] = request_id
        authoritative = complete_request(
            original, request_id=request_id, result=local_success
        )

        assert authoritative["code"] == "BACKEND_UNCERTAIN"
        assert local_success == authoritative
        assert authoritative["data"]["request_replayed"] is True
        assert authoritative["data"]["request_completion_race_resolved"] is True
        row = original.execute(
            "SELECT status,result_json FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert row["status"] == "uncertain"
    finally:
        original.close()
        recovery.close()


def test_recovery_returns_stored_success_when_original_wins(tmp_path):
    db_path = tmp_path / "dish.db"
    original = initialize_database(db_path)
    recovery = initialize_database(db_path)
    request_id = str(uuid.uuid4())
    try:
        _pending(original, request_id)
        success = result_envelope(command="prepare", data={"message": "done"})
        success.setdefault("data", {})["request_id"] = request_id
        complete_request(original, request_id=request_id, result=success)

        local_uncertain = error_envelope(
            "prepare",
            DishRuleError(
                "BACKEND_UNCERTAIN",
                "durable completion requires inspection",
                rule="service_request_result_missing",
                retryable=False,
            ),
        )
        local_uncertain.setdefault("data", {})["request_id"] = request_id
        authoritative = complete_request(
            recovery, request_id=request_id, result=local_uncertain
        )

        assert authoritative["ok"] is True
        assert local_uncertain == authoritative
        assert authoritative["data"]["request_replayed"] is True
        assert authoritative["data"]["request_completion_race_resolved"] is True
    finally:
        original.close()
        recovery.close()
