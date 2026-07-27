import json
import threading
from http.client import HTTPConnection
from urllib.parse import urlsplit

from dish_service.application import DishService
from dish_service.client import DishActionClient, DishAdminServiceClient, DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.openapi import ACTION_COMMANDS, action_openapi
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
            "start", agent="gpt", task_gid="t", kind="initial", run_id="constructor-run"
        )
        renewed = action.renew_lease(started["submission_id"])
        prepared = action.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=started["submission_id"],
            file_text=TASK,
        )
    finally:
        _stop(server, thread)
    assert started["ok"]
    assert renewed["ok"]
    assert prepared["ok"]
    assert backend.writes == 1
    assert backend.moves == 1


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
        assert arguments["anyOf"] == [
            {"required": ["run_id"]},
            {"required": ["independence_attestation"]},
        ]


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

    for status, connection_header, will_close, _payload in (
        rejected_auth,
        rejected_size,
    ):
        assert status in {400, 403}
        assert connection_header == "close"
        assert will_close
    assert rejected_auth[3]["errors"][0]["rule"] == "service_scope_forbidden"
    assert rejected_size[3]["errors"][0]["rule"] == "request_too_large"
    assert backend.writes == 0
