"""OpenAPI document for the isolated PostgreSQL Stage 4 Action port."""
from __future__ import annotations

from typing import Any

from .command_contract import ACTION_COMMANDS, COMMAND_DEFINITIONS


def postgres_action_openapi(*, server_url: str = "https://dish-postgresql.example.invalid") -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for command in ACTION_COMMANDS:
        definition = COMMAND_DEFINITIONS[command]
        request_required = definition.request_replay
        paths[f"/v1/action/{command}"] = {
            "post": {
                "operationId": f"dish_postgresql_{command.replace('-', '_')}",
                "summary": f"Run PostgreSQL-backed dish {command}",
                "description": (
                    "Replay-bound authoritative mutation; exact request identity is durable."
                    if request_required
                    else "Consistent authoritative query; request_id is not accepted."
                ),
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
                                        "required": ["run_id"] + (["request_id"] if request_required else []),
                                        "additionalProperties": False,
                                        "properties": {
                                            "run_id": {"type": "string", "format": "uuid"},
                                            **(
                                                {"request_id": {"type": "string", "format": "uuid"}}
                                                if request_required
                                                else {}
                                            ),
                                        },
                                    },
                                    "arguments": {"type": "object", "additionalProperties": True},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Canonical Dish result envelope",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ResultEnvelope"}
                            }
                        },
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
                    "required": ["ok", "command", "code", "http_status", "retryable", "data"],
                    "additionalProperties": False,
                    "properties": {
                        "ok": {"type": "boolean"},
                        "command": {"type": "string", "enum": list(ACTION_COMMANDS)},
                        "code": {"type": "string"},
                        "http_status": {"type": "integer"},
                        "retryable": {"type": "boolean"},
                        "request_replayed": {"type": "boolean"},
                        "data": {"type": "object", "additionalProperties": True},
                    },
                }
            },
        },
    }
