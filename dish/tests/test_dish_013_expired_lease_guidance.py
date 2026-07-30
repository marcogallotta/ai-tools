from __future__ import annotations

import uuid

from dish_service.application import DishService
from tests.test_dish_tool_r43_service_leases import Clock
from tests.test_dish_tool_r51_lease_authority import _principal, _service, _start
from tests.test_dish_tool_step7_verification import TASK


def _verification_with_expired_lease(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=30)
    constructor = _principal("action", "constructor-run")
    started = _start(service, constructor)
    operation_id = started["submission_id"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=constructor,
    )
    assert prepared["ok"]

    verifier = _principal("action", "verifier-run")
    reviewed = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent verification run",
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert reviewed["ok"]
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]

    clock.advance(31)
    return service, verifier, operation_id, reviewed["data"]["reviewed_identity"]


def _assert_expired_guidance(result, operation_id, *, view_path):
    assert result["allowed_actions"] == []
    data = result["data"]
    assert data["legal_next_actions"] == []
    assert data["recovery_required"] is True
    assert data["required_admin_action"] == "recover-lease"
    assert data["resolver"] == "Marco/admin recover-lease"
    assert data["admin_command"] == (
        f'dish-admin recover-lease {operation_id} '
        '--reason "<summarize why the lease is being recovered>"'
    )
    assert data["directive"] == (
        f"Tell the human to run: {data['admin_command']}\n"
        "Then wait for confirmation it succeeded before continuing — do not start a new "
        "operation; resume this same submission"
        + (
            f" with `{data['after_recovery']['legal_actions'][0]}`."
            if data["after_recovery"]["legal_actions"]
            else "."
        )
    )
    assert data["after_recovery"] == {"legal_actions": ["approve", "reject"]}
    assert data["service_access"]["state"] == "expired"
    assert data["service_access"]["rule"] == "service_lease_expired"
    assert data["service_access"]["after_recovery"] == data["after_recovery"]
    assert "recover-lease" not in result["allowed_actions"]
    assert "recover-lease" not in data["legal_next_actions"]

    view = data
    for key in view_path:
        view = view[key]
    assert view["legal_actions"] == []


def test_expired_inspect_and_read_separate_current_from_post_recovery_actions(tmp_path):
    service, verifier, operation_id, _reviewed_identity = (
        _verification_with_expired_lease(tmp_path)
    )

    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    _assert_expired_guidance(
        inspected, operation_id, view_path=("authoritative_view",)
    )

    read = service.execute_agent(
        "read",
        {"agent": "codex", "task_gid": "t"},
        principal=verifier,
    )
    _assert_expired_guidance(
        read,
        operation_id,
        view_path=("active_operation", "authoritative_view"),
    )


def test_expired_command_failure_and_exact_replay_preserve_same_guidance(tmp_path):
    service, verifier, operation_id, reviewed_identity = (
        _verification_with_expired_lease(tmp_path)
    )
    request_id = str(uuid.uuid4())
    arguments = {
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "correction": "none",
        "reviewed_identity": reviewed_identity,
        "semantic_review_complete": True,
        "provenance_complete": True,
    }

    failed = service.execute_agent(
        "approve",
        arguments,
        principal=verifier,
        request_id=request_id,
    )
    assert failed["code"] == "CONFLICT"
    assert failed["errors"][0]["rule"] == "service_lease_expired"
    _assert_expired_guidance(
        failed, operation_id, view_path=("authoritative_view",)
    )

    restarted = DishService(
        service.config,
        backend_factory=service.backend_factory,
        release_loader=service.release_loader,
        lease_now=service.lease_now,
    )
    replayed = restarted.execute_agent(
        "approve",
        arguments,
        principal=verifier,
        request_id=request_id,
    )
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    _assert_expired_guidance(
        replayed, operation_id, view_path=("authoritative_view",)
    )


def test_expired_lease_renewal_reports_private_recovery_and_post_recovery_actions(tmp_path):
    service, verifier, operation_id, _reviewed_identity = (
        _verification_with_expired_lease(tmp_path)
    )
    request_id = str(uuid.uuid4())

    failed = service.renew_lease(
        operation_id, verifier, request_id=request_id
    )
    assert failed["code"] == "CONFLICT"
    assert failed["errors"][0]["rule"] == "service_lease_expired"
    _assert_expired_guidance(
        failed, operation_id, view_path=("authoritative_view",)
    )

    replayed = service.renew_lease(
        operation_id, verifier, request_id=request_id
    )
    assert replayed["data"]["request_replayed"] is True
    _assert_expired_guidance(
        replayed, operation_id, view_path=("authoritative_view",)
    )
