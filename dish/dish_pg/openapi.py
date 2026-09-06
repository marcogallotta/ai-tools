"""OpenAPI document for the isolated PostgreSQL Stage 4 Action port."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .command_contract import (
    ACTION_COMMANDS,
    COMMAND_DEFINITIONS,
    POSTGRES_CLIENT_REQUEST_ID_SCHEMA,
    POSTGRES_CLIENT_RUN_ID_SCHEMA,
    POSTGRES_DISH_ID_SCHEMA,
    COOKED_UPDATES_COMMAND,
    SEARCH_COMMAND,
    postgres_action_argument_schema,
)


def postgres_action_openapi(*, server_url: str = "https://dish-postgresql.example.invalid") -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for command in ACTION_COMMANDS:
        definition = COMMAND_DEFINITIONS[command]
        request_required = definition.request_replay
        response_schema = (
            {"$ref": "#/components/schemas/CreateResultEnvelope"}
            if command == "create"
            else {"$ref": "#/components/schemas/ResultEnvelope"}
        )
        if command == SEARCH_COMMAND:
            description = (
                "Read-only active-title discovery. Use Search when discovering a Dish by title or "
                "partial title. If Marco supplies a canonical Dish UUID, call read(dish_id=...) "
                "directly instead of searching or browsing sections. request_id is not accepted."
            )
        elif command == COOKED_UPDATES_COMMAND:
            description = (
                "Read-only incremental cooked-evidence discovery. The first page captures a server "
                "through watermark; continuation cursors stay bound to the same generation and "
                "since/through window. A Dish can reappear when a later cook log is recorded. "
                "request_id is not accepted."
            )
        else:
            description = (
                "Replay-bound authoritative mutation; exact request identity is durable."
                if request_required
                else "Consistent authoritative query; request_id is not accepted."
            )
        paths[f"/v1/action/{command}"] = {
            "post": {
                "operationId": f"dish_postgresql_{command.replace('-', '_')}",
                "summary": f"Run PostgreSQL-backed dish {command}",
                "description": description,
                "x-openai-isConsequential": request_required,
                "security": [{"actionBearer": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["client", "arguments"],
                                "additionalProperties": False,
                                "properties": {
                                    "client": {
                                        "type": "object",
                                        "required": ["run_id"]
                                        + (["request_id"] if request_required else []),
                                        "additionalProperties": False,
                                        "properties": {
                                            "run_id": deepcopy(POSTGRES_CLIENT_RUN_ID_SCHEMA),
                                            **(
                                                {
                                                    "request_id": deepcopy(
                                                        POSTGRES_CLIENT_REQUEST_ID_SCHEMA
                                                    )
                                                }
                                                if request_required
                                                else {}
                                            ),
                                        },
                                    },
                                    "arguments": postgres_action_argument_schema(command),
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Canonical Dish result envelope",
                        "content": {"application/json": {"schema": response_schema}},
                    },
                    "401": {"description": "Bearer authentication failed before body parsing"},
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Dish PostgreSQL Action", "version": "stage-4"},
        "servers": [{"url": server_url}],
        "paths": paths,
        "components": {
            "securitySchemes": {"actionBearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "ResultEnvelope": {
                    "type": "object",
                    "required": [
                        "ok",
                        "command",
                        "code",
                        "http_status",
                        "retryable",
                        "allowed_actions",
                        "data",
                        "errors",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "ok": {"type": "boolean"},
                        "command": {"type": "string", "enum": list(ACTION_COMMANDS)},
                        "code": {"type": "string"},
                        "http_status": {"type": "integer"},
                        "task_gid": {"type": ["string", "null"]},
                        "submission_id": {
                            **deepcopy(POSTGRES_DISH_ID_SCHEMA),
                            "type": ["string", "null"],
                        },
                        "state": {"type": ["string", "null"]},
                        "retryable": {"type": "boolean"},
                        "request_replayed": {"type": "boolean"},
                        "allowed_actions": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(ACTION_COMMANDS)},
                        },
                        "data": {"type": "object", "additionalProperties": True},
                        "errors": {
                            "type": "array",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                    },
                },
                "CreateResultEnvelope": {
                    "allOf": [
                        {"$ref": "#/components/schemas/ResultEnvelope"},
                        {
                            "type": "object",
                            "properties": {
                                "data": {
                                    "type": "object",
                                    "required": ["dish_id"],
                                    "properties": {
                                        "dish_id": deepcopy(POSTGRES_DISH_ID_SCHEMA)
                                    },
                                    "additionalProperties": True,
                                }
                            },
                        },
                    ]
                },
            },
        },
    }
