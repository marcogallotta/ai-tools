from __future__ import annotations

import threading

import pytest

import dish_service.application as service_application
from dish_service.application import DishService
from dish_service.client import DishActionClient, DishServiceClient
from dish_service.command_spec import validate_action_request
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from dish_tool.cli import build_parser
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.planning import Backend, release
from tests.support.planning_intent import (
    FIRST_REQUEST,
    RUN_ID,
    SECOND_REQUEST,
    TASK_GID,
    THIRD_REQUEST,
    confirm as _confirm,
    connect as _connect,
    issue as _issue,
    planning_arguments as _planning_arguments,
    principal as _principal,
    service as _service,
)















@pytest.mark.smoke
def test_first_planning_start_only_issues_durable_confirmation(tmp_path):
    backend_calls = 0

    def backend_factory():
        nonlocal backend_calls
        backend_calls += 1
        return Backend()

    service, _backend = _service(tmp_path, backend_factory=backend_factory)
    result = _issue(service)

    assert result["code"] == "CONFIRMATION_REQUIRED"
    assert result["retryable"] is True
    assert result["allowed_actions"] == ["start"]
    assert result["submission_id"] is None
    assert result["data"]["request_id"] == FIRST_REQUEST
    assert result["data"]["required_start_kind"] == "planning"
    assert result["data"]["required_intent_basis"] == [
        "user_requested",
        "agent_override",
    ]
    challenge_id = result["data"]["intent_challenge_id"]
    assert result["data"]["planning_intent_confirmation"] == {
        "challenge_id": challenge_id,
        "status": "issued",
        "single_use": True,
        "task_gid": TASK_GID,
    }
    assert backend_calls == 0

    conn = _connect(service)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM service_leases").fetchone()[0] == 0
        row = conn.execute(
            "SELECT * FROM planning_intent_challenges WHERE challenge_id=?",
            (challenge_id,),
        ).fetchone()
        assert row["status"] == "issued"
        assert row["created_request_id"] == FIRST_REQUEST
        assert row["claimed_request_id"] is None
    finally:
        conn.close()
def test_first_call_cannot_bypass_challenge_with_lone_intent_fields(tmp_path):
    service, _ = _service(tmp_path)
    result = _issue(
        service,
        arguments=_planning_arguments(
            intent_basis="agent_override",
            override_reason="Agent decided Planning looked useful",
        ),
    )

    assert result["code"] == "CONFIRMATION_REQUIRED"
    assert result["submission_id"] is None
def test_exact_first_call_replay_returns_same_challenge(tmp_path):
    service, _ = _service(tmp_path)
    first = _issue(service)
    replay = _issue(service)

    assert replay["code"] == "CONFIRMATION_REQUIRED"
    assert replay["data"]["intent_challenge_id"] == first["data"][
        "intent_challenge_id"
    ]
    assert replay["data"]["request_replayed"] is True

    conn = _connect(service)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM planning_intent_challenges").fetchone()[0]
            == 1
        )
    finally:
        conn.close()
@pytest.mark.invariant_planning_intent
@pytest.mark.smoke
def test_fresh_user_requested_confirmation_starts_and_consumes_challenge(tmp_path):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    started = _confirm(service, challenge)

    assert started["ok"], started
    assert started["allowed_actions"] == ["prepare"]
    assert started["data"]["request_id"] == SECOND_REQUEST

    conn = _connect(service)
    try:
        row = conn.execute(
            "SELECT * FROM planning_intent_challenges WHERE challenge_id=?",
            (challenge["data"]["intent_challenge_id"],),
        ).fetchone()
        assert row["status"] == "consumed"
        assert row["claimed_request_id"] == SECOND_REQUEST
        assert row["intent_basis"] == "user_requested"
        assert row["override_reason"] is None
        assert row["operation_id"] == started["submission_id"]
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM service_leases").fetchone()[0] == 1
    finally:
        conn.close()
def test_agent_override_requires_and_persists_nonblank_reason(tmp_path):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    missing = _confirm(
        service,
        challenge,
        intent_basis="agent_override",
        override_reason="   ",
    )
    assert missing["code"] == "INVALID_ARGUMENT"
    assert missing["errors"] == [
        {
            "rule": "planning_intent_override_reason_required",
            "field": "override_reason",
        }
    ]

    started = _confirm(
        service,
        challenge,
        request_id=THIRD_REQUEST,
        intent_basis="agent_override",
        override_reason="  Task was explicitly selected for proactive planning  ",
    )
    assert started["ok"], started

    conn = _connect(service)
    try:
        row = conn.execute(
            "SELECT intent_basis,override_reason FROM planning_intent_challenges"
        ).fetchone()
        assert tuple(row) == (
            "agent_override",
            "Task was explicitly selected for proactive planning",
        )
    finally:
        conn.close()
@pytest.mark.invariant_planning_intent
@pytest.mark.smoke
def test_challenge_is_bound_to_exact_principal_task_and_single_followup(tmp_path):
    service, _ = _service(tmp_path)
    challenge = _issue(service)

    wrong_principal = _confirm(
        service,
        challenge,
        principal=_principal(
            owner_id="another-action",
            run_id="55555555-5555-4555-8555-555555555555",
        ),
    )
    assert wrong_principal["code"] == "CONFLICT"
    assert wrong_principal["errors"][0]["rule"] == "planning_intent_challenge_mismatch"

    started = _confirm(service, challenge, request_id=THIRD_REQUEST)
    assert started["ok"], started

    reused = _confirm(
        service,
        challenge,
        request_id="66666666-6666-4666-8666-666666666666",
    )
    assert reused["code"] == "CONFLICT"
    assert reused["errors"][0]["rule"] == "planning_intent_challenge_already_used"
def test_exact_replay_resumes_after_crash_between_claim_and_start(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    arguments = _planning_arguments(
        intent_challenge_id=challenge["data"]["intent_challenge_id"],
        intent_basis="user_requested",
    )
    original = service._build_agent_application
    crashed = False

    def crash_once(state, *, command, request_id):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise SystemExit("crash after durable challenge claim")
        return original(state, command=command, request_id=request_id)

    monkeypatch.setattr(service, "_build_agent_application", crash_once)
    with pytest.raises(SystemExit):
        service.execute_agent(
            "start",
            arguments,
            principal=_principal(),
            request_id=SECOND_REQUEST,
        )

    replay = service.execute_agent(
        "start",
        arguments,
        principal=_principal(),
        request_id=SECOND_REQUEST,
    )
    assert replay["ok"], replay
    assert replay["submission_id"]

    conn = _connect(service)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        row = conn.execute("SELECT status FROM planning_intent_challenges").fetchone()
        assert row["status"] == "consumed"
    finally:
        conn.close()
def test_exact_replay_converges_after_operation_commit_before_result(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    arguments = _planning_arguments(
        intent_challenge_id=challenge["data"]["intent_challenge_id"],
        intent_basis="user_requested",
    )
    original_complete = service_application.complete_request
    crashed = False

    def crash_once(conn, *, request_id, result):
        nonlocal crashed
        if result.get("code") == "OK" and not crashed:
            crashed = True
            raise SystemExit("crash before Planning request completion")
        return original_complete(conn, request_id=request_id, result=result)

    monkeypatch.setattr(service_application, "complete_request", crash_once)
    with pytest.raises(SystemExit):
        service.execute_agent(
            "start",
            arguments,
            principal=_principal(),
            request_id=SECOND_REQUEST,
        )

    replay = service.execute_agent(
        "start",
        arguments,
        principal=_principal(),
        request_id=SECOND_REQUEST,
    )
    assert replay["ok"], replay
    assert replay["data"]["request_replayed"] is True

    conn = _connect(service)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        row = conn.execute(
            "SELECT status,operation_id FROM planning_intent_challenges"
        ).fetchone()
        assert tuple(row) == ("consumed", replay["submission_id"])
    finally:
        conn.close()
