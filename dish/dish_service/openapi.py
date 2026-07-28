"""Trimmed OpenAPI document for the Custom GPT Action surface."""
from __future__ import annotations

from typing import Any

from dish_tool.validation_scope import VALIDATION_SCOPE_VALUES

from .command_spec import (
    ACTION_COMMANDS,
    CLIENT_REQUEST_ID_SCHEMA,
    CLIENT_RUN_ID_SCHEMA,
    DISH_UUID_SCHEMA,
    REPLAY_SAFE_COMMANDS,
    action_openapi_argument_schema,
)


def _action_operation_description(command: str) -> str:
    if command in REPLAY_SAFE_COMMANDS:
        return (
            "Replay-bound mutation. client.request_id is required and identifies the exact "
            "command, canonical arguments, authenticated owner, and run. Dish stores the first "
            "authoritative success or expected failure. An exact completed replay returns that "
            "stored result with data.request_replayed=true and data.request_id; reuse for "
            "different work conflicts, and matching pending or uncertain work is not executed "
            "again."
        )
    return (
        "Read-only Action. It does not accept client.request_id, create a replay record, or "
        "authorize a mutation."
    )


def action_openapi(*, server_url: str = "https://dish.example.invalid") -> dict[str, Any]:
    envelope = {
        "type": "object",
        "required": ["ok", "command", "code", "retryable", "allowed_actions", "data", "errors"],
        "properties": {
            "ok": {"type": "boolean"},
            "command": {"type": "string"},
            "code": {"type": "string"},
            "task_gid": {"type": ["string", "null"]},
            "submission_id": {
                **DISH_UUID_SCHEMA,
                "type": ["string", "null"],
            },
            "state": {"type": ["string", "null"]},
            "retryable": {
                "type": "boolean",
                "description": (
                    "Mechanical advice for a corrected or fresh call. It does not override exact "
                    "request replay: reuse the same request_id only after response loss, and never "
                    "retry BACKEND_UNCERTAIN as new work."
                ),
            },
            "allowed_actions": {"type": "array", "items": {"type": "string"}},
            "data": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "request_id": {
                        **DISH_UUID_SCHEMA,
                        "description": (
                            "The accepted mutation request UUID. Present on a replayed stored result."
                        ),
                    },
                    "request_replayed": {
                        "type": "boolean",
                        "description": (
                            "True only when this response was reconstructed from the durable result "
                            "of the exact same completed request."
                        ),
                    },
                    "validation_scope": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(VALIDATION_SCOPE_VALUES),
                        },
                        "uniqueItems": True,
                    },
                    "required_start_kind": {
                        "type": "string",
                        "enum": ["planning", "initial", "verification"],
                        "description": (
                            "Exact arguments.kind for the returned start action: planning starts "
                            "Planning from a bare task; initial starts the first Research construction "
                            "after Planning; verification starts independent Verification after Research. "
                            "A planning-to-research handoff always requires kind=initial."
                        ),
                    },
                    "material_classification": {
                        "type": ["object", "null"],
                        "description": (
                            "Prepare classification for a changed post-signoff canonical body. "
                            "When Dish overrides a requested non-material classification, "
                            "forced_material_reasons names every detected protocol reason."
                        ),
                        "required": [
                            "classified_subject",
                            "requested",
                            "effective",
                            "forced_material_reasons",
                            "route",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "classified_subject": {"type": "string"},
                            "requested": {
                                "type": "string",
                                "enum": ["material", "non-material"],
                            },
                            "effective": {
                                "type": "string",
                                "enum": ["material", "non-material"],
                            },
                            "forced_material_reasons": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Exact protocol classifier reasons that forced the effective "
                                    "classification to material; empty when no override occurred."
                                ),
                            },
                            "route": {
                                "type": "string",
                                "enum": ["verification", "signed-check-in"],
                            },
                        },
                    },
                },
            },
            "errors": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
    }
    paths: dict[str, Any] = {}
    for command in ACTION_COMMANDS:
        argument_schema = action_openapi_argument_schema(command)
        paths[f"/v1/action/{command}"] = {
            "post": {
                "operationId": f"dish_{command.replace('-', '_')}",
                "summary": f"Run dish {command}",
                "description": _action_operation_description(command),
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
                                        "required": (["run_id", "request_id"] if command in REPLAY_SAFE_COMMANDS else ["run_id"]),
                                        "additionalProperties": False,
                                        "properties": {
                                            "run_id": dict(CLIENT_RUN_ID_SCHEMA),
                                            **(
                                                {"request_id": dict(CLIENT_REQUEST_ID_SCHEMA)}
                                                if command in REPLAY_SAFE_COMMANDS
                                                else {}
                                            ),
                                        },
                                    },
                                    "arguments": argument_schema,
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Canonical dish workflow result",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ResultEnvelope"}}},
                    }
                },
            }
        }
    paths["/v1/action/leases/{operation_id}/renew"] = {
        "post": {
            "operationId": "dish_renew_lease",
            "summary": "Renew the current GPT Action operation lease",
            "description": (
                "Replay-bound mutation with the same request identity and completed, conflict, "
                "pending, and uncertain semantics as workflow mutations."
            ),
            "security": [{"actionBearer": []}],
            "parameters": [
                {
                    "name": "operation_id",
                    "in": "path",
                    "required": True,
                    "schema": dict(DISH_UUID_SCHEMA),
                }
            ],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {
                    "type": "object", "required": ["client"], "additionalProperties": False,
                    "properties": {"client": {
                        "type": "object", "required": ["run_id", "request_id"], "additionalProperties": False,
                        "properties": {
                            "run_id": dict(CLIENT_RUN_ID_SCHEMA),
                            "request_id": dict(CLIENT_REQUEST_ID_SCHEMA),
                        },
                    }},
                }}},
            },
            "responses": {"200": {"description": "Canonical lease result", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ResultEnvelope"}}}}},
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Dish GPT Action", "version": "1.0.0"},
        "servers": [{"url": server_url}],
        "paths": paths,
        "components": {
            "securitySchemes": {"actionBearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {"ResultEnvelope": envelope},
        },
    }
