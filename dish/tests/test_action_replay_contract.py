from __future__ import annotations

import copy
import json

import pytest
from pathlib import Path

from dish_service.command_spec import (
    ACTION_COMMANDS,
    ACTION_COMMAND_SPECS,
    REPLAY_SAFE_COMMANDS,
)
from dish_service.openapi import action_openapi
from dish_tool.command_identity import CONNECTED_AGENT_COMMANDS
from dish_tool.results import (
    RESULT_ENVELOPE_FIELD_SET,
    RESULT_OPENAPI_REQUIRED_FIELDS,
    result_envelope,
)
from tests.support.action_contract import (
    EXPECTED_ACTION_COMMANDS,
    EXPECTED_DISH_UUID_SCHEMA,
    EXPECTED_READ_ONLY_COMMANDS,
    EXPECTED_REPLAY_SAFE_COMMANDS,
    assert_action_openapi_contract,
    expected_run_and_request_id_paths,
    named_run_and_request_id_schemas,
)


ROOT = Path(__file__).resolve().parent.parent


def _request_schema(spec, command):
    return spec["paths"][f"/v1/action/{command}"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]


def test_openapi_documents_complete_action_replay_semantics():
    spec = action_openapi()
    for command in EXPECTED_ACTION_COMMANDS:
        post = spec["paths"][f"/v1/action/{command}"]["post"]
        client = _request_schema(spec, command)["properties"]["client"]
        description = post["description"].lower()
        if command in EXPECTED_REPLAY_SAFE_COMMANDS:
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
            assert command in EXPECTED_READ_ONLY_COMMANDS
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
    assert request_id["pattern"] == EXPECTED_DISH_UUID_SCHEMA["pattern"]
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

    assert "data.agent_guidance" in action_guide
    assert "For every Action whose imported schema requires `client.request_id`" in action_guide
    assert "This includes `inspect`" in action_guide
    assert "transport/client failure" in action_guide
    assert "up to three times after the initial attempt" in action_guide
    assert "approximately 2s, 5s, then 10s" in action_guide
    assert "Reuse the same `client.run_id`" in action_guide
    assert "the same `client.request_id`" in action_guide
    assert "As soon as any Dish envelope is received, stop blind transport retries" in action_guide
    assert "Never blindly retry `BACKEND_UNCERTAIN`" in action_guide
    assert "Truly read-only Actions" in action_guide
    assert "State-specific procedures" in action_guide

    assert "expected argument, state, authorization, and workflow failures are stored" in runtime
    assert "the first response is not labelled as a replay" in runtime
    assert "service_request_identity_conflict" in runtime
    assert "matching pending or uncertain request is never blindly executed again" in runtime
    assert "fresh UUID represents new work" in runtime



def test_connected_override_and_canonical_dish_identity_contract_are_explicit():
    action_guide = " ".join(
        (ROOT / "deploy" / "gpt-action.md").read_text(encoding="utf-8").split()
    )

    assert "standalone word `override`" in action_guide
    assert "applies only to the message that invokes it" in action_guide
    assert "does not make a disallowed transition legal" in action_guide
    assert "Dish's returned envelope remains authoritative" in action_guide
    assert "read(dish_id=<uuid>)" in action_guide
    assert "data.identity_binding" in action_guide
    assert "Never pass a Dish UUID as `submission_id`" in action_guide
    assert "section/task browsing" in action_guide
    assert "stop rather than guessing" in action_guide

def test_typed_action_policy_derives_command_and_request_id_inventory():
    assert set(CONNECTED_AGENT_COMMANDS) == set(EXPECTED_ACTION_COMMANDS)
    assert ACTION_COMMANDS == CONNECTED_AGENT_COMMANDS
    assert ACTION_COMMANDS == tuple(spec.name for spec in ACTION_COMMAND_SPECS)
    assert REPLAY_SAFE_COMMANDS == frozenset(
        spec.name for spec in ACTION_COMMAND_SPECS if spec.request_id_required
    )


def test_result_envelope_metadata_drives_client_and_openapi_shape():
    assert set(result_envelope(command="read")) == RESULT_ENVELOPE_FIELD_SET
    assert action_openapi()["components"]["schemas"]["ResultEnvelope"]["required"] == list(
        RESULT_OPENAPI_REQUIRED_FIELDS
    )


def test_every_run_and_request_id_openapi_occurrence_uses_independent_uuid_contract():
    generated = action_openapi()
    checked = json.loads((ROOT / "openapi" / "dish-action.openapi.json").read_text())

    for document in (generated, checked):
        found = named_run_and_request_id_schemas(document)
        assert set(found) == expected_run_and_request_id_paths()
        for path, schema in found.items():
            for key, expected in EXPECTED_DISH_UUID_SCHEMA.items():
                assert schema.get(key) == expected, (path, key)


def test_generated_and_checked_in_openapi_match_action_contract():
    checked = json.loads((ROOT / "openapi" / "dish-action.openapi.json").read_text())
    for document in (action_openapi(), checked):
        assert assert_action_openapi_contract(document) is None



@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document["paths"].pop("/v1/action/inspect"),
        lambda document: document["paths"]["/v1/action/read"]["post"].__setitem__(
            "x-openai-isConsequential", True
        ),
        lambda document: document["paths"]["/v1/action/create"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]["properties"]["client"][
            "properties"
        ]["run_id"].pop("pattern"),
    ],
    ids=["missing-command", "wrong-consequence", "weakened-uuid"],
)
def test_action_contract_rejects_plausible_generator_regressions(mutate):
    document = copy.deepcopy(action_openapi())
    mutate(document)

    with pytest.raises(AssertionError):
        assert assert_action_openapi_contract(document) is None

def test_connected_uuid_acceptance_remains_explicitly_reimport_gated():
    action_guide = " ".join(
        (ROOT / "deploy" / "gpt-action.md").read_text(encoding="utf-8").split()
    )

    assert "local acceptance only" in action_guide
    assert "Connected acceptance is not established until this exact schema is re-imported" in action_guide
    assert "visibly verified in the GPT editor" in action_guide
