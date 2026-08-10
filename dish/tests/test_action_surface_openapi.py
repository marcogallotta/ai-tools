import json
from dataclasses import replace
from pathlib import Path

import pytest

from dish_service import command_spec
from dish_service import openapi
from dish_service.openapi import action_openapi
from dish_tool.constants import APPROVAL_CORRECTIONS, REJECTION_ROUTES
from tests.support.action_contract import (
    EXPECTED_ACTION_COMMANDS,
    EXPECTED_CONSEQUENTIAL,
    EXPECTED_DISH_UUID_SCHEMA,
    assert_action_openapi_contract,
)


@pytest.mark.smoke
def test_action_openapi_derives_shared_metadata_from_command_definitions(monkeypatch):
    definitions = dict(command_spec.ACTION_COMMAND_DEFINITIONS)
    definitions["read"] = replace(definitions["read"], request_id_required=True)
    definitions["create"] = replace(definitions["create"], private_route="lease")
    definitions["renew-lease"] = replace(
        definitions["renew-lease"], private_route="agent"
    )
    monkeypatch.setattr(openapi, "ACTION_COMMAND_DEFINITIONS", definitions)

    spec = action_openapi(server_url="https://dish.example.test")

    read = spec["paths"]["/v1/action/read"]["post"]
    read_client = read["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["client"]
    assert read["x-openai-isConsequential"] is True
    assert "request_id" in read_client["required"]
    assert "request_id" in read_client["properties"]
    assert "replay-bound" in read["description"].lower()

    create = spec["paths"]["/v1/action/create"]["post"]
    assert create["summary"] == "Renew the current GPT Action operation lease"
    assert create["responses"]["200"]["description"] == "Canonical lease result"

    renew = spec["paths"]["/v1/action/renew-lease"]["post"]
    assert renew["summary"] == "Run dish renew-lease"
    assert renew["responses"]["200"]["description"] == "Canonical dish workflow result"


@pytest.mark.smoke
def test_trimmed_openapi_contains_only_action_workflow_and_renewal_paths():
    spec = action_openapi(server_url="https://dish.example.test")
    paths = set(spec["paths"])
    expected = {f"/v1/action/{command}" for command in EXPECTED_ACTION_COMMANDS}
    assert paths == expected
    consequential = {
        command: spec["paths"][f"/v1/action/{command}"]["post"][
            "x-openai-isConsequential"
        ]
        for command in EXPECTED_ACTION_COMMANDS
    }
    assert consequential == EXPECTED_CONSEQUENTIAL
    assert "/v1/action/leases/{operation_id}/renew" not in paths
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
        if "oneOf" in arguments:
            assert all("run_id" not in variant["required"] for variant in arguments["oneOf"])
        else:
            assert "run_id" not in arguments["required"]


@pytest.mark.smoke
def test_action_openapi_documents_client_uuid_contract_and_reject_routes():
    spec = action_openapi(server_url="https://dish.example.test")
    create_client = spec["paths"]["/v1/action/create"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["client"]
    run_id = create_client["properties"]["run_id"]
    request_id = create_client["properties"]["request_id"]
    assert run_id["format"] == "uuid"
    assert run_id["pattern"] == EXPECTED_DISH_UUID_SCHEMA["pattern"]
    assert "canonical lowercase uuid" in run_id["description"].lower()
    assert request_id["format"] == "uuid"
    assert request_id["pattern"] == EXPECTED_DISH_UUID_SCHEMA["pattern"]
    assert "one logical mutation" in request_id["description"]
    assert "lost response" in request_id["description"]

    renew_schema = spec["paths"]["/v1/action/renew-lease"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert renew_schema["required"] == ["client", "arguments"]
    renew_client = renew_schema["properties"]["client"]
    assert renew_client["properties"]["run_id"]["format"] == "uuid"
    renew_arguments = renew_schema["properties"]["arguments"]
    assert renew_arguments["required"] == ["operation_id"]
    assert renew_arguments["properties"]["operation_id"]["pattern"] == EXPECTED_DISH_UUID_SCHEMA["pattern"]
    assert "parameters" not in spec["paths"]["/v1/action/renew-lease"]["post"]

    envelope_submission = spec["components"]["schemas"]["ResultEnvelope"]["properties"]["submission_id"]
    assert envelope_submission["format"] == "uuid"
    assert envelope_submission["pattern"] == EXPECTED_DISH_UUID_SCHEMA["pattern"]
    guidance = spec["components"]["schemas"]["ResultEnvelope"]["properties"]["data"]["properties"]["agent_guidance"]
    assert guidance["properties"]["source"]["const"] == "dish"
    assert guidance["properties"]["state_specific"]["const"] is True
    assert guidance["properties"]["instructions"]["minItems"] == 1

    finding_current = spec["components"]["schemas"]["ResultEnvelope"]["properties"]["errors"]["items"]["properties"]["current"]
    assert finding_current["type"] == ["string", "object", "null"]
    assert "exact submitted value" in finding_current["description"].lower()

    prepare = spec["paths"]["/v1/action/prepare"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    prepare_attestation = prepare["properties"]["governed_change_fields"]
    assert prepare_attestation["type"] == "array"
    assert set(prepare_attestation["items"]["enum"]) == {"Decisions"}
    assert "provenance" in prepare_attestation["description"]

    reject = spec["paths"]["/v1/action/reject"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    variants = {item["properties"]["route"]["const"]: item for item in reject["oneOf"]}
    assert set(variants) == set(REJECTION_ROUTES)
    assert {"model", "file_text"}.issubset(variants["large"]["required"])
    assert "resume_status" not in variants["large"]["properties"]
    assert "independence_attestation" not in variants["large"]["properties"]
    for route in ("evidence", "human-review"):
        props = variants[route]["properties"]
        assert "resume_status" in variants[route]["required"]
        assert "file_text" not in props
        assert "model" not in props
        assert "independence_attestation" not in props
    assert "governed_change_fields" in variants["large"]["properties"]
    assert set(variants["large"]["properties"]["governed_change_fields"]["items"]["enum"]) >= {
        "Purpose", "Locks", "Decisions"
    }
    human_review_props = variants["human-review"]["properties"]
    for name in ("human_review_confirmed", "human_review_basis", "repairs_considered"):
        assert name in human_review_props
        assert name not in variants["human-review"]["required"]

    start = spec["paths"]["/v1/action/start"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    start_variants = {item["properties"]["kind"]["const"]: item for item in start["oneOf"]}
    assert "independence_attestation" in start_variants["verification"]["required"]

    approve = spec["paths"]["/v1/action/approve"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    approve_variants = {
        item["properties"]["correction"]["const"]: item for item in approve["oneOf"]
    }
    assert set(approve_variants) == set(APPROVAL_CORRECTIONS)
    assert "file_text" not in approve_variants["none"]["properties"]
    assert "file_text" in approve_variants["small"]["required"]
    for variant in approve_variants.values():
        assert "independence_attestation" not in variant["required"]
        assert "independence_attestation" not in variant["properties"]

    prepare = spec["paths"]["/v1/action/prepare"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    assert "no_blockers" not in prepare.get("properties", {})


@pytest.mark.smoke
def test_every_openapi_uuid_schema_requires_canonical_lowercase_pattern():
    from jsonschema import Draft202012Validator

    spec = action_openapi()
    uuid_schemas = []

    def collect(value):
        if isinstance(value, dict):
            if value.get("format") == "uuid":
                uuid_schemas.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(spec)
    assert uuid_schemas
    assert all(
        schema.get("pattern") == EXPECTED_DISH_UUID_SCHEMA["pattern"]
        for schema in uuid_schemas
    )
    canonical = "11111111-1111-4111-8111-111111111111"
    uppercase = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    for schema in uuid_schemas:
        validator = Draft202012Validator(schema)
        assert validator.is_valid(canonical)
        assert not validator.is_valid(uppercase)
        assert not validator.is_valid("00000000-0000-0000-0000-000000000000")


@pytest.mark.smoke
def test_checked_in_openapi_is_synchronized_with_generator():
    from pathlib import Path

    checked = json.loads((Path(__file__).parent.parent / "openapi" / "dish-action.openapi.json").read_text())
    assert checked == action_openapi()


@pytest.mark.smoke
def test_inspect_openapi_requires_request_id_in_generated_and_checked_in_schema():
    from pathlib import Path

    checked = json.loads(
        (Path(__file__).parent.parent / "openapi" / "dish-action.openapi.json").read_text()
    )
    for document in (action_openapi(), checked):
        operation = document["paths"]["/v1/action/inspect"]["post"]
        client = operation["requestBody"]["content"]["application/json"]["schema"][
            "properties"
        ]["client"]
        assert operation["operationId"] == "dish_inspect"
        assert operation["x-openai-isConsequential"] is True
        assert client["required"] == ["run_id", "request_id"]
        assert "request_id" in client["properties"]


@pytest.mark.smoke
def test_verification_targets_explain_ordinary_start_and_abandonment_scope():
    schema = action_openapi()["paths"]["/v1/action/start"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["properties"]["arguments"]
    verification = next(
        variant for variant in schema["oneOf"]
        if variant["properties"]["kind"].get("const") == "verification"
    )
    properties = verification["properties"]
    assert "ordinary Verification start" in properties["target_operation_id"]["description"]
    assert "never copy submission_id" in properties["target_operation_id"]["description"]
    assert "abandonment continuation" in properties["target_cycle_id"]["description"]



def test_generated_and_checked_in_openapi_satisfy_action_contract():
    from pathlib import Path

    checked = json.loads(
        (Path(__file__).parent.parent / "openapi" / "dish-action.openapi.json").read_text()
    )
    for document in (action_openapi(), checked):
        assert assert_action_openapi_contract(document) is None


@pytest.mark.smoke
def test_result_start_continuation_kinds_match_callable_start_variants():
    spec = action_openapi(server_url="https://dish.example.test")
    start_arguments = spec["paths"]["/v1/action/start"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["properties"]["arguments"]
    callable_start_kinds = {
        variant["properties"]["kind"]["const"] for variant in start_arguments["oneOf"]
    }

    result_start_kinds = set(
        spec["components"]["schemas"]["ResultEnvelope"]["properties"]["data"][
            "properties"
        ]["required_start_kind"]["enum"]
    )

    assert result_start_kinds == callable_start_kinds


@pytest.mark.smoke
def test_change_start_schema_requires_the_runtime_change_intent_arguments():
    spec = action_openapi(server_url="https://dish.example.test")
    start_arguments = spec["paths"]["/v1/action/start"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["properties"]["arguments"]
    change = next(
        variant
        for variant in start_arguments["oneOf"]
        if variant["properties"]["kind"].get("const") == "change"
    )

    assert {"task_gid", "agent", "kind", "change_level", "change_reason"}.issubset(
        change["required"]
    )


def test_verification_action_schema_exposes_closed_correction_and_route_values() -> None:
    checked = json.loads((Path(__file__).parent.parent / "openapi" / "dish-action.openapi.json").read_text())
    approve = checked["paths"]["/v1/action/approve"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    approve_values = {
        branch["properties"]["correction"]["enum"][0]
        for branch in approve["oneOf"]
    }
    assert approve_values == {"none", "small"}
    assert all(branch["properties"]["correction"]["enum"] == [branch["properties"]["correction"]["const"]] for branch in approve["oneOf"])

    reject = checked["paths"]["/v1/action/reject"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    reject_values = {
        branch["properties"]["route"]["enum"][0]
        for branch in reject["oneOf"]
    }
    assert reject_values == {"large", "evidence", "human-review"}
