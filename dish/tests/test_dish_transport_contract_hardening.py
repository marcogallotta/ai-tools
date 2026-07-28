from __future__ import annotations

import http.client
import io
import json
import math
import socket
import uuid
from dataclasses import replace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from dish_service import client as client_module
from dish_service.client import DishActionClient, DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.openapi import action_openapi
from dish_tool.constants import (
    MAX_REQUEST_LIFETIME_SECONDS,
    RECOVERY_SAFETY_MARGIN_SECONDS,
)
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_r45_action_surface import _running as _running_action, _stop
from tests.test_dish_tool_r54_hard_request_identity import _post, _running

RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OPERATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _config(tmp_path, **updates):
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    base = ServiceConfig(
        db_path=tmp_path / "dish.db",
        honest_root=honest,
        port=0,
        action_port=0,
        agent_token="agent-secret",
        admin_token="admin-secret",
        action_token="action-secret",
    )
    return replace(base, **updates)


@pytest.mark.parametrize("token_name", ["agent_token", "admin_token", "action_token"])
def test_runtime_rejects_surrounding_token_whitespace(tmp_path, token_name):
    config = _config(tmp_path, **{token_name: " token-secret "})
    with pytest.raises(DishRuleError) as exc:
        config.validate_runtime()
    assert exc.value.rule == "service_token_whitespace"
    assert exc.value.details["token"] == token_name.removesuffix("_token")


@pytest.mark.parametrize(
    ("path", "token", "payload"),
    [
        (
            "/v1/commands/sections",
            " agent-secret",
            {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}},
        ),
        (
            "/v1/admin/backups/create",
            "admin-secret ",
            {"client": {"run_id": RUN_ID, "request_id": REQUEST_ID}, "label": "x"},
        ),
        (
            "/v1/action/sections",
            " action-secret ",
            {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}},
        ),
    ],
)
def test_protected_routes_reject_presented_token_whitespace(
    tmp_path, path, token, payload
):
    _service, _backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(url, path, token=token, payload=payload)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 401
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "service_auth_invalid"


def test_unmodified_bearer_token_remains_accepted(tmp_path):
    _service, _backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(
            url,
            "/v1/commands/sections",
            token="agent-secret",
            payload={"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}},
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 200
    assert result["ok"] is True


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, 0.0])
def test_runtime_rejects_nonfinite_or_nonpositive_timeout(tmp_path, timeout):
    with pytest.raises(DishRuleError) as exc:
        _config(tmp_path, request_timeout_seconds=timeout).validate_runtime()
    assert exc.value.rule == "service_config_nonpositive"
    assert exc.value.details["field"] == "request_timeout_seconds"


def test_runtime_rejects_lease_ttl_shorter_than_legitimate_request(tmp_path):
    minimum = MAX_REQUEST_LIFETIME_SECONDS + RECOVERY_SAFETY_MARGIN_SECONDS
    with pytest.raises(DishRuleError) as exc:
        _config(tmp_path, lease_ttl_seconds=minimum).validate_runtime()
    assert exc.value.rule == "service_lease_ttl_too_short"
    assert exc.value.details["minimum_exclusive"] == minimum


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, 0.0])
def test_client_rejects_invalid_timeout(timeout):
    with pytest.raises(DishRuleError) as exc:
        DishServiceClient(
            "http://127.0.0.1:1", token="token-secret", run_id=RUN_ID, timeout=timeout
        )
    assert exc.value.rule == "service_timeout_invalid"


def test_client_closes_failed_http_response(monkeypatch):
    body = io.BytesIO(b'{"ok":false,"code":"INVALID_ARGUMENT"}')
    response_error = HTTPError(
        "http://dish.invalid/health", 400, "bad", {}, body
    )

    def fail(*args, **kwargs):
        raise response_error

    monkeypatch.setattr(client_module, "urlopen", fail)
    client = DishServiceClient(
        "http://dish.invalid", token="token-secret", run_id=RUN_ID
    )
    result = client.health()
    assert result["code"] == "INVALID_ARGUMENT"
    assert body.closed


def test_client_maps_abrupt_disconnect_to_service_error(monkeypatch):
    def disconnect(*args, **kwargs):
        raise http.client.RemoteDisconnected("peer closed")

    monkeypatch.setattr(client_module, "urlopen", disconnect)
    client = DishServiceClient(
        "http://dish.invalid", token="token-secret", run_id=RUN_ID
    )
    with pytest.raises(DishRuleError) as exc:
        client.health()
    assert exc.value.code == "BACKEND_REJECTED"
    assert exc.value.rule == "service_unavailable"
    assert exc.value.retryable is True


def test_short_body_is_rejected_before_json_execution(tmp_path):
    backend, server, thread, url = _running_action(tmp_path)
    parsed = urlsplit(url)
    body = json.dumps(
        {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}},
        separators=(",", ":"),
    ).encode()
    request = (
        f"POST /v1/action/sections HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Authorization: Bearer action-secret\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body) + 20}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body
    try:
        connection = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        connection.close()
    finally:
        _stop(server, thread)
    raw = b"".join(chunks)
    payload = json.loads(raw.split(b"\r\n\r\n", 1)[1])
    assert payload["code"] == "INVALID_ARGUMENT"
    assert payload["errors"][0]["rule"] == "request_body_incomplete"
    assert backend.writes == 0


@pytest.mark.parametrize(
    ("path", "token", "payload", "field"),
    [
        (
            "/v1/commands/sections",
            "agent-secret",
            {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}, "extra": True},
            "extra",
        ),
        (
            f"/v1/leases/{OPERATION_ID}/renew",
            "agent-secret",
            {"client": {"run_id": RUN_ID, "request_id": REQUEST_ID}, "extra": True},
            "extra",
        ),
        (
            "/v1/admin/backups/create",
            "admin-secret",
            {"client": {"run_id": RUN_ID, "request_id": REQUEST_ID}, "label": "x", "extra": True},
            "extra",
        ),
    ],
)
def test_private_routes_reject_unexpected_top_level_fields(
    tmp_path, path, token, payload, field
):
    _service, _backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(url, path, token=token, payload=payload)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 400
    assert result["errors"][0] == {
        "field": field,
        "rule": "request_field_unexpected",
    }


def test_read_action_rejects_undefined_request_id(tmp_path):
    _backend, server, thread, url = _running_action(tmp_path)
    try:
        action = DishActionClient(url, token="action-secret", run_id=RUN_ID)
        result = action.execute("sections", agent="gpt", request_id=REQUEST_ID)
    finally:
        _stop(server, thread)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0] == {
        "field": "client.request_id",
        "rule": "request_field_unexpected",
    }


def test_nonverification_start_rejects_independence_attestation(tmp_path):
    _backend, server, thread, url = _running_action(tmp_path)
    try:
        action = DishActionClient(url, token="action-secret", run_id=RUN_ID)
        result = action.execute(
            "start",
            agent="gpt",
            task_gid="123456789",
            kind="initial",
            independence_attestation="not applicable",
            request_id=REQUEST_ID,
        )
    finally:
        _stop(server, thread)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0] == {
        "field": "independence_attestation",
        "rule": "argument_unexpected",
    }


def _argument_variants(schema):
    return schema.get("oneOf") or [schema]


def test_action_contract_has_one_run_identity_and_precise_start_shapes():
    spec = action_openapi()
    for command in ("start", "approve", "reject"):
        arguments = spec["paths"][f"/v1/action/{command}"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
        for variant in _argument_variants(arguments):
            assert "run_id" not in variant.get("properties", {})
    start = spec["paths"]["/v1/action/start"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    variants = {variant["properties"]["kind"]["const"]: variant for variant in start["oneOf"]}
    assert "independence_attestation" in variants["verification"]["properties"]
    for kind in ("planning", "initial", "change"):
        assert "independence_attestation" not in variants[kind]["properties"]
    read_client = spec["paths"]["/v1/action/read"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["client"]
    assert "request_id" not in read_client["properties"]
    start_kind = spec["components"]["schemas"]["ResultEnvelope"]["properties"]["data"]["properties"]["required_start_kind"]
    assert "planning" in start_kind["enum"]
    assert "planning-to-research handoff always requires kind=initial" in start_kind["description"]
    assert (
        "first Research construction after Planning"
        in variants["initial"]["properties"]["kind"]["description"]
    )
    assert (
        "do not start Planning again"
        in variants["initial"]["properties"]["kind"]["description"]
    )
