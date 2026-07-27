from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.http import build_action_server, build_private_server
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend


TOKENS = {
    "agent_token": "agent-secret-12345",
    "admin_token": "admin-secret-12345",
    "action_token": "action-secret-12345",
}


def _service(tmp_path, *, backend_factory=Backend, **overrides):
    honest = tmp_path / "honest"
    honest.mkdir(parents=True)
    values = dict(
        db_path=tmp_path / "shared.db",
        honest_root=honest,
        port=0,
        action_port=0,
        **TOKENS,
    )
    values.update(overrides)
    return DishService(
        ServiceConfig(**values),
        backend_factory=backend_factory,
        release_loader=_release_loader(honest),
    )


def _post(server, path, payload, *, token="action-secret-12345"):
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=3)
    try:
        body = json.dumps(payload)
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
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"agent_token": None}, "service_token_required"),
        ({"agent_token": "same-secret", "admin_token": "same-secret"}, "service_tokens_duplicate"),
        ({"action_token": "short"}, "service_token_weak"),
        ({"bind_host": "0.0.0.0"}, "service_bind_not_loopback"),
        ({"port": 8765, "action_port": 8765}, "service_ports_duplicate"),
    ],
)
def test_runtime_configuration_fails_closed_before_listener_bind(tmp_path, overrides, rule):
    service = _service(tmp_path, **overrides)
    with pytest.raises(DishRuleError) as caught:
        build_action_server(service)
    assert caught.value.rule == rule


def test_health_reports_missing_and_duplicate_credentials(tmp_path):
    missing = _service(tmp_path / "missing", agent_token=None)
    missing_result = missing.health()
    assert not missing_result["ok"]
    assert missing_result["configuration"]["rule"] == "service_token_required"

    duplicate = _service(
        tmp_path / "duplicate", agent_token="same-secret", admin_token="same-secret"
    )
    duplicate_result = duplicate.health()
    assert not duplicate_result["ok"]
    assert duplicate_result["configuration"]["rule"] == "service_tokens_duplicate"


@pytest.mark.parametrize(
    ("command", "arguments", "rule", "field"),
    [
        ("create", {"agent": "gpt"}, "argument_required", "title"),
        ("sections", {"agent": "gpt", "junk": 1}, "argument_unexpected", "junk"),
        ("read", {"agent": "gpt", "task_gid": 123}, "argument_type_invalid", "task_gid"),
    ],
)
def test_action_server_rejects_malformed_arguments_before_workflow(
    tmp_path, command, arguments, rule, field
):
    backend_calls = {"count": 0}

    class CountingBackend(Backend):
        def read_task(self, task_gid):
            backend_calls["count"] += 1
            return super().read_task(task_gid)

    service = _service(tmp_path, backend_factory=CountingBackend)
    server = build_action_server(service)
    thread = _start(server)
    try:
        status, result = _post(
            server,
            f"/v1/action/{command}",
            {
                "client": {
                    "run_id": "run",
                    **({"request_id": "11111111-1111-4111-8111-111111111111"} if command in {"create", "start"} else {}),
                },
                "arguments": arguments,
            },
        )
    finally:
        _stop(server, thread)
    assert status == 400
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [{"field": field, "rule": rule}]
    assert backend_calls["count"] == 0


def test_unexpected_http_exception_returns_canonical_json_and_request_id(tmp_path):
    def boom():
        raise RuntimeError("private backend detail")

    service = _service(tmp_path, backend_factory=boom)
    server = build_action_server(service)
    thread = _start(server)
    try:
        status, result = _post(
            server,
            "/v1/action/sections",
            {"client": {"run_id": "run"}, "arguments": {"agent": "gpt"}},
        )
    finally:
        _stop(server, thread)
    assert status == 500
    assert result["code"] == "INTERNAL_ERROR"
    assert result["errors"][0]["rule"] == "unexpected_internal_failure"
    assert result["errors"][0]["request_id"]
    assert "private backend detail" not in json.dumps(result)
