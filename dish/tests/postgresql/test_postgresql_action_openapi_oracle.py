from __future__ import annotations

import json
from pathlib import Path

import pytest

from dish_pg.command_contract import (
    ACTION_COMMANDS,
    COMMAND_DEFINITIONS,
    CONNECTED_COMMAND_DISPOSITIONS,
    POSTGRES_CLIENT_REQUEST_ID_SCHEMA,
    POSTGRES_CLIENT_RUN_ID_SCHEMA,
    POSTGRES_DISH_ID_SCHEMA,
    POSTGRESQL_ACTION_RETIRED_COMMANDS,
    postgres_action_argument_schema,
)
from dish_pg.openapi import postgres_action_openapi
from dish_service.command_spec import ACTION_COMMAND_DEFINITIONS


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]


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
        "data",
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
    for command in ACTION_COMMANDS:
        target = COMMAND_DEFINITIONS[command]
        current = ACTION_COMMAND_DEFINITIONS[command]
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


def test_postgresql_connected_recovery_commands_are_retained() -> None:
    assert POSTGRESQL_ACTION_RETIRED_COMMANDS == ()
    for command in ACTION_COMMANDS:
        assert CONNECTED_COMMAND_DISPOSITIONS[command] == "retained"
    for command in ("proposals", "apply-proposal", "safe-reclaim"):
        assert command in ACTION_COMMANDS
        assert CONNECTED_COMMAND_DISPOSITIONS[command] == "retained"
