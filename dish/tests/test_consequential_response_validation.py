import copy
import json
import uuid

import pytest

from dish_service.client import DishActionClient, DishServiceClient
from dish_service.http import build_server
from dish_service import cli
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.lost_response import (
    ReplaceFirstResponseHTTPConnection,
    build_inspect_ready_runtime,
    inspect_fact_count,
)
from tests.support.semantic_proposal_bundle_workflow import (
    _approved_service_proposal_runtime,
)
from tests.support.thread_teardown import start_server_thread, stop_server


_NONCANONICAL_RESULT = json.dumps(
    {
        "ok": True,
        "command": "wrong-command",
        "code": "OK",
        "task_gid": None,
        "submission_id": None,
        "state": None,
        "retryable": False,
        "allowed_actions": [],
        "data": {},
        "errors": [],
    },
    separators=(",", ":"),
).encode("utf-8")

_AMBIGUOUS_RESPONSE_BODIES = [
    pytest.param(b'{"ok":', id="invalid-json"),
    pytest.param(b"\xff", id="unreadable-encoding"),
    pytest.param(b"", id="empty-response"),
    pytest.param(_NONCANONICAL_RESULT, id="noncanonical-result"),
]

_REQUIRED_HTTP_REPLACEMENTS = [
    pytest.param(b'{"ok":', id="invalid-json"),
    pytest.param(_NONCANONICAL_RESULT, id="noncanonical-result"),
]


@pytest.mark.parametrize(
    ("client_type", "token"),
    [
        (DishServiceClient, "agent-secret"),
        (DishActionClient, "action-secret"),
    ],
)
def test_consequential_request_serialization_failure_is_not_ambiguous(
    monkeypatch, client_type, token
):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("serialization failure must happen before network dispatch")

    monkeypatch.setattr(
        "dish_service._client_transport.http.client.HTTPConnection", unexpected_connection
    )
    client = client_type(
        "http://127.0.0.1:1", token=token, run_id=str(uuid.uuid4())
    )

    with pytest.raises(TypeError, match="not JSON serializable"):
        client.execute(
            "inspect",
            {"agent": "codex", "submission_id": object()},
        )


@pytest.mark.parametrize(
    ("client_type", "token"),
    [
        (DishServiceClient, "agent-secret"),
        (DishActionClient, "action-secret"),
    ],
)
def test_consequential_connect_failure_remains_safe_to_retry(
    monkeypatch, client_type, token
):
    class RefusingConnection:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            raise OSError("connection refused before dispatch")

        def close(self):
            pass

    monkeypatch.setattr(
        "dish_service._client_transport.http.client.HTTPConnection", RefusingConnection
    )
    client = client_type(
        "http://127.0.0.1:1", token=token, run_id=str(uuid.uuid4())
    )

    with pytest.raises(DishRuleError) as caught:
        client.execute(
            "inspect",
            {"agent": "codex", "submission_id": str(uuid.uuid4())},
        )
    assert caught.value.code == "BACKEND_REJECTED"
    assert caught.value.rule == "service_unavailable"
    assert caught.value.retryable is True


def _replace_first_response(monkeypatch, replacement: bytes) -> None:
    ReplaceFirstResponseHTTPConnection.reset(replacement=replacement)
    monkeypatch.setattr(
        "dish_service._client_transport.http.client.HTTPConnection",
        ReplaceFirstResponseHTTPConnection,
    )


def _assert_uncertain(result, *, request_id: str, run_id: str) -> None:
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["retryable"] is False
    assert result["data"] == {
        "message": "the request may have reached the service, but no authoritative response was received",
        "request_id": request_id,
        "run_id": run_id,
        "request_replay_required": True,
        "required_next_action": "retry_exact_request",
        "safe_to_retry": False,
    }
    assert result["errors"] == [{"rule": "service_response_ambiguous"}]


def _expected_exact_replay(authoritative, *, request_id: str):
    expected = copy.deepcopy(authoritative)
    expected.setdefault("data", {})["request_replayed"] = True
    expected["data"]["request_id"] = request_id
    return expected


def _proposal_state(service, proposal_id: str):
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
        return proposal["status"], cycle_count
    finally:
        conn.close()


@pytest.mark.parametrize("replacement", _AMBIGUOUS_RESPONSE_BODIES)
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
def test_inspect_untrustworthy_response_requires_exact_replay(
    tmp_path,
    monkeypatch,
    replacement,
    client_type,
    token,
    owner_id,
    explicit_request_id,
):
    run_id = str(uuid.uuid4())
    service, server, thread, url, operation_id = build_inspect_ready_runtime(
        tmp_path, owner_id=owner_id, run_id=run_id
    )
    _replace_first_response(monkeypatch, replacement)
    client = client_type(url, token=token, run_id=run_id)
    arguments = {"agent": "codex", "submission_id": operation_id}
    try:
        first = client.execute("inspect", arguments, request_id=explicit_request_id)
        assert len(ReplaceFirstResponseHTTPConnection.captured_payloads) == 1
        assert len(ReplaceFirstResponseHTTPConnection.authoritative_responses) == 1
        request_id = first["data"]["request_id"]
        authoritative = ReplaceFirstResponseHTTPConnection.authoritative_responses[0]
        replayed = client.execute("inspect", arguments, request_id=request_id)
        mismatch = client.execute(
            "inspect", {**arguments, "agent": "gpt"}, request_id=request_id
        )
    finally:
        stop_server(server, thread)

    transmitted = ReplaceFirstResponseHTTPConnection.captured_payloads[0]
    assert transmitted["client"] == {"run_id": run_id, "request_id": request_id}
    if explicit_request_id is None:
        assert str(uuid.UUID(request_id)) == request_id
    else:
        assert request_id == explicit_request_id
    _assert_uncertain(first, request_id=request_id, run_id=run_id)
    assert replayed == _expected_exact_replay(
        authoritative, request_id=request_id
    )
    assert mismatch["code"] == "CONFLICT"
    assert mismatch["errors"][0]["rule"] == "service_request_identity_conflict"
    assert inspect_fact_count(service, operation_id) == 1


@pytest.mark.parametrize("replacement", _AMBIGUOUS_RESPONSE_BODIES)
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
def test_apply_proposal_untrustworthy_response_requires_exact_replay(
    tmp_path, monkeypatch, replacement, client_type, token, explicit_request_id
):
    service, backend, proposal_id, task_gid = _approved_service_proposal_runtime(
        tmp_path
    )
    server = build_server(service)
    thread = start_server_thread(
        server, daemon=True, name="apply-proposal-ambiguous-response"
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
        _replace_first_response(monkeypatch, replacement)
        arguments = {
            "proposal_id": proposal_id,
            "agent": "gpt",
            "model": "gpt-5.6-sol",
        }
        first = client.execute(
            "apply-proposal", arguments, request_id=explicit_request_id
        )
        assert len(ReplaceFirstResponseHTTPConnection.captured_payloads) == 1
        assert len(ReplaceFirstResponseHTTPConnection.authoritative_responses) == 1
        request_id = first["data"]["request_id"]
        authoritative = ReplaceFirstResponseHTTPConnection.authoritative_responses[0]
        writes_after_first = backend.writes
        state_after_first = _proposal_state(service, proposal_id)
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

    transmitted = ReplaceFirstResponseHTTPConnection.captured_payloads[0]
    assert transmitted["client"] == {"run_id": run_id, "request_id": request_id}
    if explicit_request_id is None:
        assert str(uuid.UUID(request_id)) == request_id
    else:
        assert request_id == explicit_request_id
    _assert_uncertain(first, request_id=request_id, run_id=run_id)
    assert replayed == _expected_exact_replay(
        authoritative, request_id=request_id
    )
    assert replayed["data"]["proposal"]["status"] == "applied"
    assert backend.writes == writes_after_first
    assert _proposal_state(service, proposal_id) == state_after_first == ("applied", 2)
    assert mismatch["code"] == "CONFLICT"
    assert mismatch["errors"][0]["rule"] == "service_request_identity_conflict"


@pytest.mark.parametrize("replacement", _REQUIRED_HTTP_REPLACEMENTS)
def test_inspect_cli_exposes_exact_replay_for_untrustworthy_response(
    tmp_path, monkeypatch, capsys, replacement
):
    run_id = str(uuid.uuid4())
    service, server, thread, url, operation_id = build_inspect_ready_runtime(
        tmp_path, owner_id="cli", run_id=run_id
    )
    _replace_first_response(monkeypatch, replacement)
    client = DishServiceClient(url, token="agent-secret", run_id=run_id)
    arguments = ["inspect", operation_id, "--agent", "codex"]
    try:
        status = cli.main(arguments, application=client)
        failed = json.loads(capsys.readouterr().out)
        request_id = failed["data"]["request_id"]
        replay_status = cli.main(failed["data"]["replay_argv"][1:], application=client)
        replayed = json.loads(capsys.readouterr().out)
    finally:
        stop_server(server, thread)

    assert status != 0
    assert failed["data"]["replay_argv"] == [
        "dish",
        *arguments,
        "--request-id",
        request_id,
    ]
    assert failed["data"]["replay_environment"] == {"DISH_CLIENT_RUN_ID": run_id}
    assert failed["data"]["replay_command"].endswith(f"--request-id {request_id}")
    assert replay_status == 0
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    assert inspect_fact_count(service, operation_id) == 1


@pytest.mark.parametrize("replacement", _REQUIRED_HTTP_REPLACEMENTS)
def test_apply_proposal_cli_exposes_exact_replay_for_untrustworthy_response(
    tmp_path, monkeypatch, capsys, replacement
):
    service, backend, proposal_id, task_gid = _approved_service_proposal_runtime(
        tmp_path
    )
    server = build_server(service)
    thread = start_server_thread(
        server, daemon=True, name="apply-proposal-cli-ambiguous-response"
    )
    host, port = server.server_address
    run_id = str(uuid.uuid4())
    client = DishServiceClient(
        f"http://{host}:{port}", token="agent-secret", run_id=run_id
    )
    request_id = "44444444-4444-4444-8444-444444444444"
    arguments = [
        "apply-proposal",
        proposal_id,
        "--agent",
        "gpt",
        "--model",
        "gpt-5.6-sol",
        f"--request-id={request_id}",
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
        _replace_first_response(monkeypatch, replacement)
        status = cli.main(arguments, application=client)
        failed = json.loads(capsys.readouterr().out)
        writes_after_first = backend.writes
        state_after_first = _proposal_state(service, proposal_id)
        replay_status = cli.main(failed["data"]["replay_argv"][1:], application=client)
        replayed = json.loads(capsys.readouterr().out)
    finally:
        stop_server(server, thread)

    assert status != 0
    assert failed["data"]["request_id"] == request_id
    assert failed["data"]["replay_argv"] == ["dish", *arguments]
    assert failed["data"]["replay_environment"] == {"DISH_CLIENT_RUN_ID": run_id}
    assert failed["data"]["replay_command"].endswith(f"--request-id={request_id}")
    assert replay_status == 0
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    assert backend.writes == writes_after_first
    assert _proposal_state(service, proposal_id) == state_after_first == ("applied", 2)
