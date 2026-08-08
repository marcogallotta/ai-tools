import json
import uuid

import pytest

from dish_service.client import DishActionClient, DishServiceClient
from dish_service.http import build_server
from dish_service import cli
from dish_tool.database import initialize_database
from tests.support.lost_response import (
    LoseFirstResponseHTTPConnection,
    build_inspect_ready_runtime,
    inspect_fact_count,
)
from tests.support.semantic_proposal_bundle_workflow import (
    _approved_service_proposal_runtime,
)
from tests.support.thread_teardown import start_server_thread, stop_server


@pytest.mark.parametrize(
    ("client_type", "token", "owner_id", "explicit_request_id"),
    [
        (DishServiceClient, "agent-secret", "cli", None),
        (
            DishActionClient,
            "action-secret",
            "gpt-action",
            "33333333-3333-4333-8333-333333333333",
        ),
    ],
)
def test_inspect_lost_response_surfaces_transmitted_identity_for_exact_replay(
    tmp_path,
    monkeypatch,
    client_type,
    token,
    owner_id,
    explicit_request_id,
):
    run_id = str(uuid.uuid4())
    service, server, thread, url, operation_id = build_inspect_ready_runtime(
        tmp_path, owner_id=owner_id, run_id=run_id
    )
    LoseFirstResponseHTTPConnection.reset()
    monkeypatch.setattr(
        "dish_service._client_transport.http.client.HTTPConnection",
        LoseFirstResponseHTTPConnection,
    )
    client = client_type(url, token=token, run_id=run_id)
    arguments = {"agent": "codex", "submission_id": operation_id}
    try:
        first = client.execute("inspect", arguments, request_id=explicit_request_id)
        assert len(LoseFirstResponseHTTPConnection.captured_payloads) == 1
        request_id = first["data"]["request_id"]
        replayed = client.execute("inspect", arguments, request_id=request_id)
        mismatch = client.execute(
            "inspect",
            {**arguments, "agent": "gpt"},
            request_id=request_id,
        )
    finally:
        stop_server(server, thread)

    transmitted = LoseFirstResponseHTTPConnection.captured_payloads[0]
    assert transmitted["client"]["request_id"] == request_id
    assert transmitted["client"]["run_id"] == run_id
    if explicit_request_id is not None:
        assert request_id == explicit_request_id
    else:
        assert str(uuid.UUID(request_id)) == request_id
    assert first["code"] == "BACKEND_UNCERTAIN"
    assert first["retryable"] is False
    assert first["data"]["request_replay_required"] is True
    assert first["data"]["required_next_action"] == "retry_exact_request"
    assert first["data"]["safe_to_retry"] is False
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    assert mismatch["code"] == "CONFLICT"
    assert mismatch["errors"][0]["rule"] == "service_request_identity_conflict"

    assert inspect_fact_count(service, operation_id) == 1


def test_inspect_cli_prints_exact_replay_command_after_lost_response(
    tmp_path, monkeypatch, capsys
):
    run_id = str(uuid.uuid4())
    service, server, thread, url, operation_id = build_inspect_ready_runtime(
        tmp_path, owner_id="cli", run_id=run_id
    )
    LoseFirstResponseHTTPConnection.reset()
    monkeypatch.setattr(
        "dish_service._client_transport.http.client.HTTPConnection",
        LoseFirstResponseHTTPConnection,
    )
    client = DishServiceClient(url, token="agent-secret", run_id=run_id)
    arguments = ["inspect", operation_id, "--agent", "codex"]
    try:
        status = cli.main(arguments, application=client)
        failed = json.loads(capsys.readouterr().out)
        assert len(LoseFirstResponseHTTPConnection.captured_payloads) == 1
        replay_argv = failed["data"]["replay_argv"]
        replay_status = cli.main(replay_argv[1:], application=client)
        replayed = json.loads(capsys.readouterr().out)
    finally:
        stop_server(server, thread)

    request_id = failed["data"]["request_id"]
    assert status != 0
    assert failed["retryable"] is False
    assert replay_argv == ["dish", *arguments, "--request-id", request_id]
    assert failed["data"]["replay_environment"] == {"DISH_CLIENT_RUN_ID": run_id}
    assert failed["data"]["replay_command"].endswith(f"--request-id {request_id}")
    assert replay_status == 0
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id

    assert inspect_fact_count(service, operation_id) == 1


@pytest.mark.parametrize(
    ("client_type", "token", "explicit_request_id"),
    [
        (DishServiceClient, "agent-secret", None),
        (
            DishActionClient,
            "action-secret",
            "33333333-3333-4333-8333-333333333333",
        ),
    ],
)
def test_apply_proposal_lost_response_replays_without_second_application(
    tmp_path, monkeypatch, client_type, token, explicit_request_id
):
    service, backend, proposal_id, task_gid = _approved_service_proposal_runtime(
        tmp_path
    )
    server = build_server(service)
    thread = start_server_thread(
        server, daemon=True, name="apply-proposal-lost-response"
    )
    host, port = server.server_address
    run_id = str(uuid.uuid4())
    client = client_type(f"http://{host}:{port}", token=token, run_id=run_id)
    try:
        available = client.execute(
            "start",
            {
                "agent": "gpt",
                "task_gid": task_gid,
                "kind": "verification",
                "independence_attestation": "independent",
            },
        )
        assert available["allowed_actions"] == ["apply-proposal"]
        LoseFirstResponseHTTPConnection.reset()
        monkeypatch.setattr(
            "dish_service._client_transport.http.client.HTTPConnection",
            LoseFirstResponseHTTPConnection,
        )
        arguments = {
            "proposal_id": proposal_id,
            "agent": "gpt",
            "model": "gpt-5.6-sol",
        }
        first = client.execute(
            "apply-proposal", arguments, request_id=explicit_request_id
        )
        assert len(LoseFirstResponseHTTPConnection.captured_payloads) == 1
        request_id = first["data"]["request_id"]
        writes_after_first = backend.writes
        replayed = client.execute(
            "apply-proposal", arguments, request_id=request_id
        )
        mismatch = client.execute(
            "apply-proposal",
            {**arguments, "model": "different-model"},
            request_id=request_id,
        )
    finally:
        stop_server(server, thread)

    transmitted = LoseFirstResponseHTTPConnection.captured_payloads[0]
    assert transmitted["client"]["request_id"] == request_id
    assert transmitted["client"]["run_id"] == run_id
    if explicit_request_id is not None:
        assert request_id == explicit_request_id
    else:
        assert str(uuid.UUID(request_id)) == request_id
    assert first["code"] == "BACKEND_UNCERTAIN"
    assert first["retryable"] is False
    assert first["data"]["request_replay_required"] is True
    assert first["data"]["safe_to_retry"] is False
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    assert replayed["data"]["proposal"]["status"] == "applied"
    assert backend.writes == writes_after_first
    assert mismatch["code"] == "CONFLICT"
    assert mismatch["errors"][0]["rule"] == "service_request_identity_conflict"

    conn = initialize_database(service.config.db_path)
    try:
        proposal = conn.execute(
            "SELECT operation_id,status FROM semantic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        cycle_count = conn.execute(
            "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?",
            (proposal["operation_id"],),
        ).fetchone()[0]
        assert proposal["status"] == "applied"
        assert cycle_count == 2
    finally:
        conn.close()


def test_apply_proposal_cli_preserves_explicit_identity_in_replay_command(
    tmp_path, monkeypatch, capsys
):
    service, backend, proposal_id, task_gid = _approved_service_proposal_runtime(
        tmp_path
    )
    server = build_server(service)
    thread = start_server_thread(
        server, daemon=True, name="apply-proposal-cli-lost-response"
    )
    host, port = server.server_address
    client = DishServiceClient(
        f"http://{host}:{port}", token="agent-secret", run_id=str(uuid.uuid4())
    )
    explicit_request_id = "44444444-4444-4444-8444-444444444444"
    arguments = [
        "apply-proposal",
        proposal_id,
        "--agent",
        "gpt",
        "--model",
        "gpt-5.6-sol",
        f"--request-id={explicit_request_id}",
    ]
    try:
        available = client.execute(
            "start",
            {
                "agent": "gpt",
                "task_gid": task_gid,
                "kind": "verification",
                "independence_attestation": "independent",
            },
        )
        assert available["allowed_actions"] == ["apply-proposal"]
        LoseFirstResponseHTTPConnection.reset()
        monkeypatch.setattr(
            "dish_service._client_transport.http.client.HTTPConnection",
            LoseFirstResponseHTTPConnection,
        )
        status = cli.main(arguments, application=client)
        failed = json.loads(capsys.readouterr().out)
        assert len(LoseFirstResponseHTTPConnection.captured_payloads) == 1
        writes_after_first = backend.writes
        replay_status = cli.main(failed["data"]["replay_argv"][1:], application=client)
        replayed = json.loads(capsys.readouterr().out)
    finally:
        stop_server(server, thread)

    assert status != 0
    assert failed["data"]["request_id"] == explicit_request_id
    assert failed["data"]["replay_argv"] == ["dish", *arguments]
    assert failed["data"]["replay_environment"] == {
        "DISH_CLIENT_RUN_ID": client.run_id
    }
    assert failed["data"]["replay_command"].endswith(
        f"--request-id={explicit_request_id}"
    )
    assert replay_status == 0
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == explicit_request_id
    assert backend.writes == writes_after_first

    conn = initialize_database(service.config.db_path)
    try:
        operation_id = conn.execute(
            "SELECT operation_id FROM semantic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()[0]
        cycle_count = conn.execute(
            "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
        assert cycle_count == 2
    finally:
        conn.close()
