import pytest
import json

from dish_service.openapi import ACTION_COMMANDS, action_openapi


@pytest.mark.smoke
def test_trimmed_openapi_contains_only_action_workflow_and_renewal_paths():
    spec = action_openapi(server_url="https://dish.example.test")
    paths = set(spec["paths"])
    expected = {f"/v1/action/{command}" for command in ACTION_COMMANDS}
    assert paths == expected
    consequential = {
        command: spec["paths"][f"/v1/action/{command}"]["post"][
            "x-openai-isConsequential"
        ]
        for command in ACTION_COMMANDS
    }
    assert consequential == {
        "create": True,
        "sections": False,
        "read": False,
        "inspect": False,
        "start": True,
        "prepare": True,
        "approve": True,
        "reject": True,
        "submit": True,
        "renew-lease": True,
    }
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
    from dish_service.identifiers import CANONICAL_DISH_UUID_PATTERN

    spec = action_openapi(server_url="https://dish.example.test")
    create_client = spec["paths"]["/v1/action/create"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["client"]
    run_id = create_client["properties"]["run_id"]
    request_id = create_client["properties"]["request_id"]
    assert run_id["format"] == "uuid"
    assert run_id["pattern"] == CANONICAL_DISH_UUID_PATTERN
    assert "canonical lowercase uuid" in run_id["description"].lower()
    assert request_id["format"] == "uuid"
    assert request_id["pattern"] == CANONICAL_DISH_UUID_PATTERN
    assert "one logical mutation" in request_id["description"]
    assert "lost response" in request_id["description"]

    renew_schema = spec["paths"]["/v1/action/renew-lease"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert renew_schema["required"] == ["client", "arguments"]
    renew_client = renew_schema["properties"]["client"]
    assert renew_client["properties"]["run_id"]["format"] == "uuid"
    renew_arguments = renew_schema["properties"]["arguments"]
    assert renew_arguments["required"] == ["operation_id"]
    assert renew_arguments["properties"]["operation_id"]["pattern"] == CANONICAL_DISH_UUID_PATTERN
    assert "parameters" not in spec["paths"]["/v1/action/renew-lease"]["post"]

    envelope_submission = spec["components"]["schemas"]["ResultEnvelope"]["properties"]["submission_id"]
    assert envelope_submission["format"] == "uuid"
    assert envelope_submission["pattern"] == CANONICAL_DISH_UUID_PATTERN

    reject = spec["paths"]["/v1/action/reject"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    variants = {item["properties"]["route"]["const"]: item for item in reject["oneOf"]}
    assert set(variants) == {"large", "evidence", "human-review"}
    assert {"model", "file_text"}.issubset(variants["large"]["required"])
    assert "resume_status" not in variants["large"]["properties"]
    assert "independence_attestation" not in variants["large"]["properties"]
    for route in ("evidence", "human-review"):
        props = variants[route]["properties"]
        assert "resume_status" in variants[route]["required"]
        assert "file_text" not in props
        assert "model" not in props
        assert "independence_attestation" not in props

    start = spec["paths"]["/v1/action/start"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    start_variants = {item["properties"]["kind"]["const"]: item for item in start["oneOf"]}
    assert "independence_attestation" in start_variants["verification"]["required"]

    approve = spec["paths"]["/v1/action/approve"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["arguments"]
    approve_variants = {
        item["properties"]["correction"]["const"]: item for item in approve["oneOf"]
    }
    assert set(approve_variants) == {"none", "small"}
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

    from dish_service.identifiers import (
        CANONICAL_DISH_UUID_PATTERN,
        NIL_DISH_UUID,
    )

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
        schema.get("pattern") == CANONICAL_DISH_UUID_PATTERN
        for schema in uuid_schemas
    )
    canonical = "11111111-1111-4111-8111-111111111111"
    uppercase = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    for schema in uuid_schemas:
        validator = Draft202012Validator(schema)
        assert validator.is_valid(canonical)
        assert not validator.is_valid(uppercase)
        assert not validator.is_valid(NIL_DISH_UUID)


@pytest.mark.smoke
def test_checked_in_openapi_matches_generator():
    from pathlib import Path

    checked = json.loads((Path(__file__).parent.parent / "openapi" / "dish-action.openapi.json").read_text())
    assert checked == action_openapi()

