from __future__ import annotations

import json
from pathlib import Path

import pytest

from dish_pg.openapi import postgres_action_openapi


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ACTIONS = (
    "create",
    "sections",
    "section-tasks",
    "read",
    "inspect",
    "start",
    "prepare",
    "approve",
    "reject",
    "submit",
    "renew-lease",
)
EXPECTED_OPERATIONS = {
    "/v1/action/create": ("dish_postgresql_create", True, ["run_id", "request_id"]),
    "/v1/action/sections": ("dish_postgresql_sections", False, ["run_id"]),
    "/v1/action/section-tasks": (
        "dish_postgresql_section_tasks",
        False,
        ["run_id"],
    ),
    "/v1/action/read": ("dish_postgresql_read", False, ["run_id"]),
    "/v1/action/inspect": (
        "dish_postgresql_inspect",
        True,
        ["run_id", "request_id"],
    ),
    "/v1/action/start": (
        "dish_postgresql_start",
        True,
        ["run_id", "request_id"],
    ),
    "/v1/action/prepare": (
        "dish_postgresql_prepare",
        True,
        ["run_id", "request_id"],
    ),
    "/v1/action/approve": (
        "dish_postgresql_approve",
        True,
        ["run_id", "request_id"],
    ),
    "/v1/action/reject": (
        "dish_postgresql_reject",
        True,
        ["run_id", "request_id"],
    ),
    "/v1/action/submit": (
        "dish_postgresql_submit",
        True,
        ["run_id", "request_id"],
    ),
    "/v1/action/renew-lease": (
        "dish_postgresql_renew_lease",
        True,
        ["run_id", "request_id"],
    ),
}


def _assert_literal_postgresql_action_contract(document: dict[str, object]) -> None:
    paths = document["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == set(EXPECTED_OPERATIONS)

    for path, (operation_id, consequential, client_required) in EXPECTED_OPERATIONS.items():
        operation = paths[path]["post"]
        assert operation["operationId"] == operation_id
        assert operation["x-openai-isConsequential"] is consequential
        assert operation["security"] == [{"actionBearer": []}]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert schema["required"] == ["client", "arguments"]
        assert schema["additionalProperties"] is False
        client = schema["properties"]["client"]
        assert client["required"] == client_required
        assert client["additionalProperties"] is False
        assert client["properties"]["run_id"] == {
            "type": "string",
            "format": "uuid",
        }
        if consequential:
            assert client["properties"]["request_id"] == {
                "type": "string",
                "format": "uuid",
            }
        else:
            assert "request_id" not in client["properties"]

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
        "enum": list(EXPECTED_ACTIONS),
    }
    assert document["components"]["securitySchemes"] == {
        "actionBearer": {"type": "http", "scheme": "bearer"}
    }


def test_generated_postgresql_action_openapi_matches_literal_oracle() -> None:
    _assert_literal_postgresql_action_contract(postgres_action_openapi())


def test_checked_in_postgresql_action_openapi_matches_literal_oracle() -> None:
    checked_in = json.loads(
        (ROOT / "openapi" / "dish-postgresql-action.openapi.json").read_text(
            encoding="utf-8"
        )
    )
    _assert_literal_postgresql_action_contract(checked_in)
    assert checked_in == postgres_action_openapi()
