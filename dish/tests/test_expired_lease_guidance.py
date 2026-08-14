from __future__ import annotations

import uuid

from dish_service.application import DishService
from tests.support.service_leases import Clock
from tests.support.lease_authority import _principal, _service, _start
from tests.support.verification import TASK


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


def _assert_same_run_revival_guidance(result, operation_id, *, view_path):
    assert result["allowed_actions"] == ["renew-lease"]
    data = result["data"]
    assert data["legal_next_actions"] == ["renew-lease"]
    assert data.get("recovery_required") is not True
    assert data.get("required_admin_action") is None
    assert data["agent_action"] == {
        "command": "renew-lease",
        "arguments": {"operation_id": operation_id},
    }
    assert "no Marco/admin recovery" in data["legal_next_step"]
    assert "no new run_id" in data["legal_next_step"]
    assert data["service_access"]["state"] == "expired_same_run_revivable"
    assert data["service_access"]["rule"] == "service_lease_same_run_revivable"

    view = data
    for key in view_path:
        view = view[key]
    assert view["legal_actions"] == ["renew-lease"]


def test_expired_inspect_and_read_offer_same_run_connected_revival(tmp_path):
    service, verifier, operation_id, _reviewed_identity = (
        _verification_with_expired_lease(tmp_path)
    )

    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["ok"] is True
    assert inspected["code"] == "OK"
    _assert_same_run_revival_guidance(
        inspected, operation_id, view_path=("authoritative_view",)
    )

    read = service.execute_agent(
        "read",
        {"agent": "codex", "task_gid": "t"},
        principal=verifier,
    )
    assert read["ok"] is True
    assert read["code"] == "OK"
    _assert_same_run_revival_guidance(
        read,
        operation_id,
        view_path=("active_operation", "authoritative_view"),
    )


def test_expired_mutation_transparently_revives_same_run_and_exact_replay_is_stable(tmp_path):
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

    approved = service.execute_agent(
        "approve",
        arguments,
        principal=verifier,
        request_id=request_id,
    )
    assert approved["ok"] is True
    assert approved["submission_id"] == operation_id

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
    assert replayed["ok"] is True
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    assert replayed["submission_id"] == operation_id


def test_expired_lease_renewal_revives_same_run_and_exact_request_replays(tmp_path):
    service, verifier, operation_id, _reviewed_identity = (
        _verification_with_expired_lease(tmp_path)
    )
    request_id = str(uuid.uuid4())

    revived = service.renew_lease(
        operation_id, verifier, request_id=request_id
    )
    assert revived["ok"] is True
    assert revived["data"]["service_lease"]["run_id"] == verifier.run_id

    replayed = service.renew_lease(
        operation_id, verifier, request_id=request_id
    )
    assert replayed["ok"] is True
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    assert replayed["data"]["service_lease"]["lease_id"] == revived["data"]["service_lease"]["lease_id"]
