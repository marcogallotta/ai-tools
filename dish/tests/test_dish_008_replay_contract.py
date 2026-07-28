from __future__ import annotations

import json
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
            assert len(post["description"]) <= 300
            assert "binds command, arguments, owner, and client.run_id" in description
            assert "stored success or failure across restarts" in description
            assert "exact replays" in description
            assert "changed reuse conflicts" in description
            assert "pending or uncertain" in description
            assert "not rerun" in description
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
        "binds command, arguments, owner, and client.run_id",
        "stored success or failure across restarts",
        "exact replays",
        "changed reuse conflicts",
        "pending or uncertain",
        "not rerun",
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


def test_generated_and_checked_in_operation_descriptions_fit_importer_limit():
    generated = action_openapi()
    checked = json.loads(
        (ROOT / "openapi" / "dish-action.openapi.json").read_text(encoding="utf-8")
    )
    for spec in (generated, checked):
        for path, item in spec["paths"].items():
            description = item["post"]["description"]
            assert len(description) <= 300, (path, len(description))


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


def test_every_run_and_request_id_openapi_occurrence_uses_shared_uuid_authority():
    import json

    from dish_service.command_spec import ACTION_COMMANDS, REPLAY_SAFE_COMMANDS
    from dish_service.identifiers import CANONICAL_DISH_UUID_SCHEMA

    generated = action_openapi()
    checked = json.loads((ROOT / "openapi" / "dish-action.openapi.json").read_text())

    def named_identifier_schemas(document):
        found = {}

        def collect(value, path=()):
            if isinstance(value, dict):
                for key, child in value.items():
                    child_path = (*path, key)
                    if key in {"run_id", "request_id"}:
                        found[child_path] = child
                    collect(child, child_path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    collect(child, (*path, str(index)))

        collect(document)
        return found

    expected_run_paths = {
        (
            "paths",
            f"/v1/action/{command}",
            "post",
            "requestBody",
            "content",
            "application/json",
            "schema",
            "properties",
            "client",
            "properties",
            "run_id",
        )
        for command in ACTION_COMMANDS
    }
    expected_request_paths = {
        (
            "paths",
            f"/v1/action/{command}",
            "post",
            "requestBody",
            "content",
            "application/json",
            "schema",
            "properties",
            "client",
            "properties",
            "request_id",
        )
        for command in REPLAY_SAFE_COMMANDS
    }
    expected_request_paths.add(
        (
            "components",
            "schemas",
            "ResultEnvelope",
            "properties",
            "data",
            "properties",
            "request_id",
        )
    )
    expected_paths = expected_run_paths | expected_request_paths

    for document in (generated, checked):
        found = named_identifier_schemas(document)
        assert set(found) == expected_paths
        for path, schema in found.items():
            for key, expected in CANONICAL_DISH_UUID_SCHEMA.items():
                assert schema.get(key) == expected, (path, key)


def test_connected_uuid_acceptance_remains_explicitly_reimport_gated():
    action_guide = " ".join(
        (ROOT / "deploy" / "gpt-action.md").read_text(encoding="utf-8").split()
    )

    assert "local acceptance only" in action_guide
    assert "Connected acceptance is not established until this exact schema is re-imported" in action_guide
    assert "visibly verified in the GPT editor" in action_guide
