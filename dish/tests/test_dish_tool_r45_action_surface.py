import json
import threading
import uuid
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService
from dish_service.client import DishActionClient, DishAdminServiceClient, DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.identifiers import validate_identifier_fields
from dish_service.openapi import ACTION_COMMANDS, action_openapi
from dish_tool.backend import map_backend_exception
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import RequestPhase
from dish_tool.results import error_envelope
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend, TASK


def _running(tmp_path, *, max_body=2 * 1024 * 1024):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            max_body_bytes=max_body,
            agent_token="cli-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return backend, server, thread, f"http://{host}:{port}"


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _raw_post(url, path, *, token, body):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, response.getheader("Connection"), response.will_close, payload
    finally:
        connection.close()


def test_cli_and_action_receive_identical_workflow_results(tmp_path):
    _backend, server, thread, url = _running(tmp_path)
    try:
        cli = DishServiceClient(url, token="cli-secret", run_id="cli-run")
        action = DishActionClient(url, token="action-secret", run_id="action-run")
        cli_result = cli.execute("sections", agent="gpt")
        action_result = action.execute("sections", agent="gpt")
    finally:
        _stop(server, thread)
    assert cli_result == action_result


def test_action_credential_is_rejected_from_cli_and_admin_surfaces(tmp_path):
    _backend, server, thread, url = _running(tmp_path)
    try:
        wrong_cli = DishServiceClient(url, token="action-secret", run_id="action-run")
        wrong_admin = DishAdminServiceClient(url, token="action-secret", run_id="action-run")
        cli_result = wrong_cli.execute("read", agent="gpt", task_gid="t")
        admin_result = wrong_admin.execute("discard", submission_id="x", reason="x")
    finally:
        _stop(server, thread)
    assert cli_result["errors"][0]["rule"] == "service_scope_forbidden"
    assert admin_result["errors"][0]["rule"] == "service_scope_forbidden"


def test_cli_credential_is_rejected_from_action_surface(tmp_path):
    _backend, server, thread, url = _running(tmp_path)
    try:
        wrong = DishActionClient(url, token="cli-secret", run_id="cli-run")
        result = wrong.execute("sections", agent="gpt")
    finally:
        _stop(server, thread)
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "service_scope_forbidden"


def test_action_surface_supports_leased_start_prepare_and_heartbeat(tmp_path):
    backend, server, thread, url = _running(tmp_path)
    try:
        action = DishActionClient(url, token="action-secret", run_id="constructor-run")
        started = action.execute(
            "start", agent="gpt", task_gid="123456789", kind="initial"
        )
        renewed = action.renew_lease(started["submission_id"])
        prepared = action.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=started["submission_id"],
            file_text=TASK,
        )
        inspected = action.execute(
            "inspect", agent="gpt", submission_id=started["submission_id"],
        )
    finally:
        _stop(server, thread)
    assert started["ok"]
    assert renewed["ok"]
    assert prepared["ok"]
    assert inspected["data"]["operation"]["run_id"] == "constructor-run"
    assert inspected["data"]["actors"]["run_id"] == "constructor-run"
    assert backend.writes == 1
    assert backend.moves == 1


@pytest.mark.parametrize(
    "task_gid",
    ["not-a-gid", "123abc", "", " ", "-1"],
)
def test_action_rejects_malformed_task_gid_before_backend_call(tmp_path, task_gid):
    backend, server, thread, url = _running(tmp_path)
    calls = 0
    original = backend.read_task

    def counted_read(gid):
        nonlocal calls
        calls += 1
        return original(gid)

    backend.read_task = counted_read
    try:
        action = DishActionClient(url, token="action-secret", run_id="run")
        result = action.execute("read", agent="gpt", task_gid=task_gid)
    finally:
        _stop(server, thread)

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert result["errors"] == [
        {"field": "task_gid", "rule": "numeric_identifier_required"}
    ]
    assert calls == 0


def test_action_distinguishes_nonexistent_numeric_gid_and_reaches_backend(tmp_path):
    backend, server, thread, url = _running(tmp_path)
    calls = 0

    def missing(gid):
        nonlocal calls
        calls += 1
        raise BackendFailure(
            "BACKEND_REJECTED",
            "private Asana 404 response body",
            status=404,
            retryable=True,
        )

    backend.read_task = missing
    try:
        action = DishActionClient(url, token="action-secret", run_id="run")
        result = action.execute("read", agent="gpt", task_gid="999999999")
    finally:
        _stop(server, thread)

    assert result["code"] == "NOT_FOUND"
    assert result["retryable"] is False
    assert result["errors"][0]["rule"] == "task_not_found"
    assert "private Asana" not in json.dumps(result)
    assert calls == 1


def test_valid_numeric_gid_reaches_action_backend_path(tmp_path):
    backend, server, thread, url = _running(tmp_path)
    calls = 0
    original = backend.read_task

    def counted_read(gid):
        nonlocal calls
        calls += 1
        return original(gid)

    backend.read_task = counted_read
    try:
        action = DishActionClient(url, token="action-secret", run_id="run")
        result = action.execute("read", agent="gpt", task_gid="123456789")
    finally:
        _stop(server, thread)

    assert result["ok"]
    assert calls == 2


@pytest.mark.parametrize("submission_id", ["not-an-operation", "", " "])
def test_action_rejects_malformed_submission_id_before_database_routing(
    tmp_path, submission_id
):
    _backend, server, thread, url = _running(tmp_path)
    try:
        action = DishActionClient(url, token="action-secret", run_id="run")
        result = action.execute(
            "inspect", agent="gpt", submission_id=submission_id
        )
    finally:
        _stop(server, thread)

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert result["errors"] == [
        {"field": "submission_id", "rule": "uuid_identifier_required"}
    ]


def test_action_rejects_malformed_lease_operation_id(tmp_path):
    _backend, server, thread, url = _running(tmp_path)
    try:
        action = DishActionClient(url, token="action-secret", run_id="run")
        result = action.renew_lease("not-an-operation")
    finally:
        _stop(server, thread)

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"field": "operation_id", "rule": "uuid_identifier_required"}
    ]


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("project_gid", "project", "numeric_identifier_required"),
        ("section_gid", "-123", "numeric_identifier_required"),
        ("operation_id", "operation", "uuid_identifier_required"),
        ("cycle_id", "cycle", "uuid_identifier_required"),
        ("verification_cycle_id", "cycle", "uuid_identifier_required"),
    ],
)
def test_all_http_identifier_field_classes_use_strict_grammar(field, value, rule):
    with pytest.raises(DishRuleError) as caught:
        validate_identifier_fields({field: value})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.rule == rule
    assert caught.value.details == {"field": field}


def test_action_sanitizes_raw_backend_rejection(tmp_path):
    backend, server, thread, url = _running(tmp_path)

    def rejected(_gid):
        raise BackendFailure(
            "BACKEND_REJECTED",
            "Asana API error (400) https://app.asana.com/private raw-body",
            status=400,
            retryable=True,
        )

    backend.read_task = rejected
    try:
        action = DishActionClient(url, token="action-secret", run_id="run")
        result = action.execute("read", agent="gpt", task_gid="123456789")
    finally:
        _stop(server, thread)

    rendered = json.dumps(result)
    assert result["code"] == "BACKEND_REJECTED"
    assert result["data"]["message"] == "backend request was rejected"
    assert "asana.com" not in rendered.lower()
    assert "raw-body" not in rendered


def test_inaccessible_backend_identifier_is_distinct_and_non_retryable():
    class Forbidden(Exception):
        status = 403
        body = "private access policy detail"
        reason = "Forbidden"

    failure = map_backend_exception(
        Forbidden(),
        phase=RequestPhase.RESPONSE_RECEIVED,
        context="task 123456789",
    )
    result = error_envelope("read", failure, task_gid="123456789")

    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is False
    assert result["errors"] == [{"rule": "backend_access_denied"}]
    assert result["data"]["message"] == "backend request was rejected"
    assert "private access policy" not in json.dumps(result)


def test_canonical_operation_uuid_is_accepted_by_boundary_validator():
    operation_id = str(uuid.uuid4())
    validate_identifier_fields({"submission_id": operation_id})


def test_trimmed_openapi_contains_only_action_workflow_and_renewal_paths():
    spec = action_openapi(server_url="https://dish.example.test")
    paths = set(spec["paths"])
    expected = {f"/v1/action/{command}" for command in ACTION_COMMANDS}
    expected.add("/v1/action/leases/{operation_id}/renew")
    assert paths == expected
    rendered = json.dumps(spec).lower()
    assert "/admin" not in rendered
    assert "recover" not in rendered
    assert "discard" not in rendered
    assert "migrate" not in rendered
    assert "asana" not in rendered
    assert "secret" not in rendered
    for command in ("approve", "reject"):
        arguments = spec["paths"][f"/v1/action/{command}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]["arguments"]
        assert "anyOf" not in arguments
        assert "run_id" not in arguments["required"]


def test_checked_in_openapi_matches_generator():
    from pathlib import Path

    checked = json.loads((Path(__file__).parent.parent / "openapi" / "dish-action.openapi.json").read_text())
    assert checked == action_openapi()


def test_action_request_limit_applies_before_workflow_execution(tmp_path):
    backend, server, thread, url = _running(tmp_path, max_body=80)
    try:
        action = DishActionClient(url, token="action-secret", run_id="run")
        result = action.execute("create", agent="gpt", title="x" * 500)
    finally:
        _stop(server, thread)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "request_too_large"
    assert backend.writes == 0


def test_pre_body_auth_and_size_rejections_close_the_connection(tmp_path):
    backend, server, thread, url = _running(tmp_path, max_body=80)
    valid_body = json.dumps(
        {"client": {"run_id": "run"}, "arguments": {"agent": "gpt"}}
    )
    oversized_body = json.dumps(
        {"client": {"run_id": "run"}, "arguments": {"title": "x" * 500}}
    )
    try:
        rejected_auth = _raw_post(
            url, "/v1/action/sections", token="cli-secret", body=valid_body
        )
        rejected_size = _raw_post(
            url, "/v1/action/create", token="action-secret", body=oversized_body
        )
    finally:
        _stop(server, thread)

    assert rejected_auth[0] == 403
    assert rejected_size[0] == 200
    for _status, connection_header, will_close, _payload in (
        rejected_auth,
        rejected_size,
    ):
        assert connection_header == "close"
        assert will_close
    assert rejected_auth[3]["errors"][0]["rule"] == "service_scope_forbidden"
    assert rejected_size[3]["errors"][0]["rule"] == "request_too_large"
    assert backend.writes == 0
