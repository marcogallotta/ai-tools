from __future__ import annotations

import json
from pathlib import Path

import pytest

from dish_pg.command_contract import (
    ACTION_COMMANDS,
    COMMAND_DEFINITIONS,
    CONNECTED_ACTION_COMMANDS_NOT_YET_PORTED,
    CONNECTED_COMMAND_DISPOSITIONS,
    COOKED_COMMAND,
    POSTGRES_CLIENT_REQUEST_ID_SCHEMA,
    POSTGRES_CLIENT_RUN_ID_SCHEMA,
    POSTGRES_DISH_ID_SCHEMA,
    POSTGRESQL_ACTION_ADDED_COMMANDS,
    POSTGRESQL_ACTION_RETIRED_COMMANDS,
    SEARCH_COMMAND,
    SEARCH_PAGE_SIZE_DEFAULT,
    SEARCH_PAGE_SIZE_MAX,
    SEARCH_QUERY_MAX_LENGTH,
    postgres_action_argument_schema,
    validate_postgres_action_request,
)
from dish_pg.openapi import postgres_action_openapi
from dish_service.command_spec import ACTION_COMMAND_DEFINITIONS
from dish_tool.errors import DishRuleError


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_RUN_ID = "11111111-1111-4111-8111-111111111111"
DISCOVERY_SECTION_ID = "22222222-2222-4222-8222-222222222222"


def _assert_postgresql_action_contract(document: dict[str, object]) -> None:
    paths = document["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == {f"/v1/action/{command}" for command in ACTION_COMMANDS}

    for command in ACTION_COMMANDS:
        definition = COMMAND_DEFINITIONS[command]
        path = f"/v1/action/{command}"
        operation = paths[path]["post"]
        assert operation["operationId"] == f"dish_postgresql_{command.replace('-', '_')}"
        assert operation["x-openai-isConsequential"] is definition.request_replay
        assert operation["security"] == [{"actionBearer": []}]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert schema["required"] == ["client", "arguments"]
        assert schema["additionalProperties"] is False
        client = schema["properties"]["client"]
        assert client["required"] == (
            ["run_id", "request_id"] if definition.request_replay else ["run_id"]
        )
        assert client["additionalProperties"] is False
        assert client["properties"]["run_id"] == POSTGRES_CLIENT_RUN_ID_SCHEMA
        if definition.request_replay:
            assert client["properties"]["request_id"] == POSTGRES_CLIENT_REQUEST_ID_SCHEMA
        else:
            assert "request_id" not in client["properties"]
        assert schema["properties"]["arguments"] == postgres_action_argument_schema(command)

    envelope = document["components"]["schemas"]["ResultEnvelope"]
    assert envelope["required"] == [
        "ok",
        "command",
        "code",
        "http_status",
        "retryable",
        "allowed_actions",
        "data",
        "errors",
    ]
    assert envelope["properties"]["command"] == {
        "type": "string",
        "enum": list(ACTION_COMMANDS),
    }
    assert document["components"]["securitySchemes"] == {
        "actionBearer": {"type": "http", "scheme": "bearer"}
    }
    create_response = document["components"]["schemas"]["CreateResultEnvelope"]
    create_data = create_response["allOf"][1]["properties"]["data"]
    assert create_data["required"] == ["dish_id"]
    assert create_data["properties"]["dish_id"] == POSTGRES_DISH_ID_SCHEMA


def test_postgresql_action_metadata_reuses_current_principal_and_replay_policy() -> None:
    for command, current in ACTION_COMMAND_DEFINITIONS.items():
        if command in CONNECTED_ACTION_COMMANDS_NOT_YET_PORTED:
            continue
        target = COMMAND_DEFINITIONS[command]
        assert target.principal == current.principal
        assert target.request_replay is current.request_id_required


def test_generated_postgresql_action_openapi_matches_command_contract() -> None:
    _assert_postgresql_action_contract(postgres_action_openapi())


def test_checked_in_postgresql_action_openapi_matches_command_contract() -> None:
    checked_in = json.loads(
        (ROOT / "openapi" / "dish-postgresql-action.openapi.json").read_text(
            encoding="utf-8"
        )
    )
    _assert_postgresql_action_contract(checked_in)
    assert checked_in == postgres_action_openapi()


def test_postgresql_action_identity_fields_are_canonical_with_local_gid_aliases() -> None:
    section_schema = postgres_action_argument_schema("section-tasks")
    assert "section_id" in section_schema["properties"]
    assert "section_gid" in section_schema["properties"]
    assert section_schema["oneOf"] == [
        {"required": ["section_id"]},
        {"required": ["section_gid"]},
    ]

    start_schema = postgres_action_argument_schema("start")
    assert start_schema["discriminator"] == {"propertyName": "kind"}
    for variant in start_schema["oneOf"]:
        assert "dish_id" in variant["properties"]
        assert "task_gid" in variant["properties"]
        assert variant["oneOf"] == [
            {"required": ["dish_id"]},
            {"required": ["task_gid"]},
        ]

    read_schema = postgres_action_argument_schema("read")
    assert read_schema["oneOf"][0]["required"] == ["dish_id", "agent"]


def test_postgresql_discovery_reads_reuse_one_stable_run_id_and_expose_pagination() -> None:
    sections_request = {
        "client": {"run_id": DISCOVERY_RUN_ID},
        "arguments": {"agent": "gpt"},
    }
    first_client, first_arguments = validate_postgres_action_request(
        "sections", sections_request
    )
    repeated_client, repeated_arguments = validate_postgres_action_request(
        "sections", sections_request
    )
    section_client, section_arguments = validate_postgres_action_request(
        "section-tasks",
        {
            "client": {"run_id": DISCOVERY_RUN_ID},
            "arguments": {
                "section_id": DISCOVERY_SECTION_ID,
                "agent": "gpt",
                "cursor": "opaque-next-page-token",
            },
        },
    )

    assert first_client == repeated_client == section_client == {
        "run_id": DISCOVERY_RUN_ID
    }
    assert first_arguments == repeated_arguments == {"agent": "gpt"}
    assert section_arguments == {
        "section_id": DISCOVERY_SECTION_ID,
        "agent": "gpt",
        "cursor": "opaque-next-page-token",
    }
    section_schema = postgres_action_argument_schema("section-tasks")
    assert "cursor" in section_schema["properties"]
    assert "request_id" not in POSTGRES_CLIENT_RUN_ID_SCHEMA


def test_postgresql_discovery_reads_reject_malformed_run_id_consistently() -> None:
    requests = (
        ("sections", {"agent": "gpt"}),
        (
            "section-tasks",
            {"section_id": DISCOVERY_SECTION_ID, "agent": "gpt"},
        ),
    )
    for command, arguments in requests:
        with pytest.raises(DishRuleError) as error:
            validate_postgres_action_request(
                command,
                {
                    "client": {"run_id": "NOT-A-CANONICAL-UUID"},
                    "arguments": arguments,
                },
            )
        assert error.value.code == "INVALID_ARGUMENT"
        assert error.value.rule == "uuid_identifier_required"
        assert error.value.details["field"] == "client.run_id"


def test_postgresql_search_action_is_read_only_bounded_and_reuses_stable_run_id() -> None:
    assert POSTGRESQL_ACTION_ADDED_COMMANDS == (SEARCH_COMMAND, COOKED_COMMAND)
    assert SEARCH_COMMAND in ACTION_COMMANDS
    definition = COMMAND_DEFINITIONS[SEARCH_COMMAND]
    assert definition.profile == "Q"
    assert definition.request_replay is False

    schema = postgres_action_argument_schema(SEARCH_COMMAND)
    assert schema["required"] == ["query", "agent"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["query"]["maxLength"] == SEARCH_QUERY_MAX_LENGTH
    assert schema["properties"]["page_size"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": SEARCH_PAGE_SIZE_MAX,
        "default": SEARCH_PAGE_SIZE_DEFAULT,
    }
    assert "cursor" in schema["properties"]

    client, arguments = validate_postgres_action_request(
        SEARCH_COMMAND,
        {
            "client": {"run_id": DISCOVERY_RUN_ID},
            "arguments": {
                "query": "  Potato  ",
                "agent": "gpt",
                "page_size": 2,
            },
        },
    )
    assert client == {"run_id": DISCOVERY_RUN_ID}
    assert arguments == {"query": "Potato", "agent": "gpt", "page_size": 2}
    assert "request_id" not in client


def test_postgresql_cooked_action_is_replay_bound_and_canonical_only() -> None:
    assert COOKED_COMMAND in ACTION_COMMANDS
    assert COOKED_COMMAND not in ACTION_COMMAND_DEFINITIONS
    definition = COMMAND_DEFINITIONS[COOKED_COMMAND]
    assert definition.request_replay is True
    assert definition.action_exposed is True
    assert definition.task_required is True
    assert definition.operation_required is False

    schema = postgres_action_argument_schema(COOKED_COMMAND)
    assert schema["required"] == ["dish_id", "agent"]
    assert schema["additionalProperties"] is False
    assert "task_gid" not in schema["properties"]

    client, arguments = validate_postgres_action_request(
        COOKED_COMMAND,
        {
            "client": {
                "run_id": DISCOVERY_RUN_ID,
                "request_id": "33333333-3333-4333-8333-333333333333",
            },
            "arguments": {"dish_id": DISCOVERY_SECTION_ID, "agent": "gpt"},
        },
    )
    assert client["request_id"] == "33333333-3333-4333-8333-333333333333"
    assert arguments == {"dish_id": DISCOVERY_SECTION_ID, "agent": "gpt"}


def test_postgresql_search_action_rejects_bad_pagination_and_unknown_fields() -> None:
    base = {"client": {"run_id": DISCOVERY_RUN_ID}}
    for arguments, rule in (
        ({"query": "potato", "agent": "gpt", "page_size": 0}, "argument_range_invalid"),
        ({"query": "potato", "agent": "gpt", "page_size": 101}, "argument_range_invalid"),
        ({"query": "potato", "agent": "gpt", "cursor": ""}, "argument_type_invalid"),
        ({"query": "potato", "agent": "gpt", "body": "no"}, "argument_field_forbidden"),
    ):
        with pytest.raises(DishRuleError) as error:
            validate_postgres_action_request(
                SEARCH_COMMAND,
                {**base, "arguments": arguments},
            )
        assert error.value.rule == rule


def test_postgresql_connected_recovery_commands_are_retained() -> None:
    assert POSTGRESQL_ACTION_RETIRED_COMMANDS == ()
    for command in ACTION_COMMANDS:
        assert CONNECTED_COMMAND_DISPOSITIONS[command] == "retained"
    for command in ("proposals", "apply-proposal", "safe-reclaim"):
        assert command in ACTION_COMMANDS
        assert CONNECTED_COMMAND_DISPOSITIONS[command] == "retained"
