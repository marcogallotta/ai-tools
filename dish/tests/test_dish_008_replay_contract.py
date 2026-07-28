from __future__ import annotations

from pathlib import Path

from dish_service.command_spec import ACTION_COMMANDS, REPLAY_SAFE_COMMANDS
from dish_service.identifiers import CANONICAL_DISH_UUID_PATTERN
from dish_service.openapi import action_openapi


ROOT = Path(__file__).resolve().parent.parent


def _request_schema(spec, command):
    return spec["paths"][f"/v1/action/{command}"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]


def test_openapi_documents_complete_action_replay_semantics():
    spec = action_openapi()
    for command in ACTION_COMMANDS:
        post = spec["paths"][f"/v1/action/{command}"]["post"]
        client = _request_schema(spec, command)["properties"]["client"]
        description = post["description"].lower()
        if command in REPLAY_SAFE_COMMANDS:
            assert "request_id" in client["required"]
            assert "exact command, canonical arguments" in description
            assert "authenticated owner" in description
            assert "client.run_id" in description
            assert "including expected failures" in description
            assert "preserves it across service restart" in description
            assert "exact replay with the same identity" in description
            assert "changed arguments" in description
            assert "different command" in description
            assert "different authenticated owner" in description
            assert "different run" in description
            assert "service_request_identity_conflict" in description
            assert "pending or uncertain" in description
            assert "not executed again" in description
            assert "fail-closed" in description
        else:
            assert "request_id" not in client["properties"]
            assert "request_id" not in client["required"]
            assert "read-only" in description
            assert "does not accept client.request_id" in description

    renew = spec["paths"]["/v1/action/renew-lease"]["post"]
    renew_client = renew["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["client"]
    assert "request_id" in renew_client["required"]
    renew_description = renew["description"].lower()
    for phrase in (
        "exact command, canonical arguments",
        "authenticated owner",
        "client.run_id",
        "including expected failures",
        "across service restart",
        "exact replay with the same identity",
        "changed arguments",
        "different command",
        "different authenticated owner",
        "different run",
        "service_request_identity_conflict",
        "pending or uncertain",
        "not executed again",
        "fail-closed",
    ):
        assert phrase in renew_description

    envelope = spec["components"]["schemas"]["ResultEnvelope"]["properties"]
    assert envelope["data"]["properties"]["request_replayed"]["type"] == "boolean"
    request_id = envelope["data"]["properties"]["request_id"]
    assert request_id["format"] == "uuid"
    assert request_id["pattern"] == CANONICAL_DISH_UUID_PATTERN
    assert "fresh call" in envelope["retryable"]["description"]
    assert "does not override exact request replay" in envelope["retryable"]["description"]


def test_action_and_runtime_docs_preserve_replay_inventory_and_decision_rules():
    action_guide = " ".join(
        (ROOT / "deploy" / "gpt-action.md").read_text(encoding="utf-8").split()
    )
    runtime = " ".join(
        (ROOT / "docs" / "runtime-contract.md").read_text(encoding="utf-8").split()
    )

    assert "`create`, `start`, `prepare`, `approve`, `reject`, `submit`" in action_guide
    assert "Read-only `sections`, `read`, and `inspect` do not accept a request ID" in action_guide
    assert "first authoritative success or expected failure" in action_guide
    assert "Reusing the UUID for different work conflicts" in action_guide
    assert "pending or uncertain request is not executed again" in action_guide

    assert "expected argument, state, authorization, and workflow failures are stored" in runtime
    assert "the first response is not labelled as a replay" in runtime
    assert "service_request_identity_conflict" in runtime
    assert "matching pending or uncertain request is never blindly executed again" in runtime
    assert "fresh UUID represents new work" in runtime
