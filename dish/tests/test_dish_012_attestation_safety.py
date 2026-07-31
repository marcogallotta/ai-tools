from __future__ import annotations

import json
import uuid

import pytest

from dish_service.command_spec import validate_action_request
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import validate_independence_attestation
from tests.support.action_http import _raw_post, _running, _stop
from tests.support.lease_authority import _principal, _service, _start
from tests.support.verification import TASK


UNSAFE_ATTESTATIONS = (
    "independent\nsecond line",
    "independent\rsecond line",
    "independent\tsecond field",
    "independent\x00hidden",
    "independent\x1fhidden",
    "independent\u200bhidden",
    "independent\u2060hidden",
    "independent\u2028second line",
    "independent\u2029second paragraph",
    "independent\ud800hidden",
)


@pytest.mark.parametrize("attestation", UNSAFE_ATTESTATIONS)
def test_independence_attestation_rejects_structural_characters(attestation):
    with pytest.raises(DishRuleError) as caught:
        validate_independence_attestation(attestation)

    error = caught.value
    assert error.code == "INVALID_ARGUMENT"
    assert error.rule == "independence_attestation_invalid_characters"
    assert error.retryable is True
    assert error.details == {"field": "independence_attestation"}


@pytest.mark.parametrize("command", ("start",))
def test_action_boundary_rejects_unsafe_attestation_for_every_public_entry(command):
    arguments = {
        "task_gid": "1216963171560192",
        "agent": "gpt",
        "kind": "verification",
        "independence_attestation": "independent\nsecond line",
    }
    request = {
        "client": {
            "run_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
        },
        "arguments": arguments,
    }

    with pytest.raises(DishRuleError) as caught:
        validate_action_request(command, request)

    assert caught.value.rule == "independence_attestation_invalid_characters"
    assert caught.value.retryable is True
    assert caught.value.details == {"field": "independence_attestation"}


def test_action_http_rejects_unsafe_attestation_before_replay_or_backend_state(tmp_path):
    backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    body = json.dumps({
        "client": {
            "run_id": str(uuid.uuid4()),
            "request_id": request_id,
        },
        "arguments": {
            "task_gid": "1216963171560192",
            "agent": "gpt",
            "kind": "verification",
            "independence_attestation": "independent\nsecond line",
        },
    })
    try:
        status, _connection, _will_close, result = _raw_post(
            url,
            "/v1/action/start",
            token="action-secret",
            body=body,
        )
    finally:
        _stop(server, thread)

    assert status == 200
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is True
    assert result["errors"] == [{
        "rule": "independence_attestation_invalid_characters",
        "field": "independence_attestation",
    }]
    assert backend.writes == 0
    assert backend.moves == 0

    conn = initialize_database(server.service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM verification_cycles").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM service_requests WHERE request_id=?", (request_id,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def _prepared_verification_service(tmp_path):
    service, backend = _service(tmp_path)
    constructor = _principal("action", "constructor-run")
    started = _start(service, constructor)
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=constructor,
    )
    assert prepared["ok"]
    return service, backend, started["submission_id"]


def test_invalid_attestation_creates_no_durable_verification_evidence(tmp_path):
    service, backend, operation_id = _prepared_verification_service(tmp_path)
    request_id = str(uuid.uuid4())
    verifier = _principal("action", "verifier-run")

    conn = initialize_database(service.config.db_path)
    try:
        before = {
            "operations": conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
            "cycles": conn.execute("SELECT COUNT(*) FROM verification_cycles").fetchone()[0],
            "executions": conn.execute("SELECT COUNT(*) FROM operation_executions").fetchone()[0],
            "actor_facts": conn.execute("SELECT COUNT(*) FROM operation_actor_facts").fetchone()[0],
        }
        cycle_before = conn.execute(
            "SELECT verifier_agent, run_id, independence_attestation FROM verification_cycles "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()

    writes_before = backend.writes
    moves_before = backend.moves
    result = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent\nsecond line",
        },
        principal=verifier,
        request_id=request_id,
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is True
    assert result["errors"] == [
        {
            "rule": "independence_attestation_invalid_characters",
            "field": "independence_attestation",
        }
    ]
    assert backend.writes == writes_before
    assert backend.moves == moves_before

    conn = initialize_database(service.config.db_path)
    try:
        after = {
            "operations": conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
            "cycles": conn.execute("SELECT COUNT(*) FROM verification_cycles").fetchone()[0],
            "executions": conn.execute("SELECT COUNT(*) FROM operation_executions").fetchone()[0],
            "actor_facts": conn.execute("SELECT COUNT(*) FROM operation_actor_facts").fetchone()[0],
        }
        cycle_after = conn.execute(
            "SELECT verifier_agent, run_id, independence_attestation FROM verification_cycles "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        request_count = conn.execute(
            "SELECT COUNT(*) FROM service_requests WHERE request_id=?", (request_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert after == before
    assert tuple(cycle_after) == tuple(cycle_before) == (None, None, None)
    assert request_count == 0
    assert "second line" not in json.dumps(result)


def test_valid_unicode_attestation_is_preserved_and_exposed(tmp_path):
    service, _backend, operation_id = _prepared_verification_service(tmp_path)
    verifier = _principal("action", "verifier-run")
    attestation = "Vérification indépendante — مصادر séparées"

    started = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": attestation,
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert started["ok"]

    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["ok"]
    assert attestation in json.dumps(inspected, ensure_ascii=False)

    conn = initialize_database(service.config.db_path)
    try:
        row = conn.execute(
            "SELECT independence_attestation FROM verification_cycles WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["independence_attestation"] == attestation
