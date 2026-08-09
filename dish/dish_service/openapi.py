"""Trimmed OpenAPI document for the Custom GPT Action surface."""
from __future__ import annotations

from typing import Any

from dish_tool.results import RESULT_OPENAPI_REQUIRED_FIELDS
from dish_tool.validation_scope import VALIDATION_SCOPE_VALUES

from .command_spec import (
    ACTION_COMMAND_DEFINITIONS,
    CLIENT_REQUEST_ID_SCHEMA,
    CLIENT_RUN_ID_SCHEMA,
    DISH_UUID_SCHEMA,
    ActionCommandSpec,
    action_openapi_argument_schema,
)


REPLAY_MUTATION_DESCRIPTION = (
    "Replay-bound: request_id binds command, arguments, owner, and client.run_id. Exact replays "
    "return the stored success or failure across restarts. Changed reuse conflicts. Pending or "
    "uncertain work stays fail-closed and is not rerun."
)


def _action_operation_description(spec: ActionCommandSpec) -> str:
    if spec.request_id_required:
        return REPLAY_MUTATION_DESCRIPTION
    return (
        "Read-only Action. It does not accept client.request_id, create a replay record, or "
        "authorize a mutation."
    )


def action_openapi(*, server_url: str = "https://dish.example.invalid") -> dict[str, Any]:
    envelope = {
        "type": "object",
        "required": list(RESULT_OPENAPI_REQUIRED_FIELDS),
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
                    "agent_guidance": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source", "state_specific", "instructions"],
                        "properties": {
                            "source": {"type": "string", "const": "dish"},
                            "state_specific": {"type": "boolean", "const": True},
                            "instructions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                        },
                        "description": (
                            "Immediate operating guidance rendered by Dish from this exact result. "
                            "It is contextual guidance, not a second source of workflow legality; "
                            "allowed_actions remains authoritative."
                        ),
                    },
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
                    "intent_challenge_id": {
                        **DISH_UUID_SCHEMA,
                        "description": (
                            "Durable single-use Planning intent challenge. Repeat start with a "
                            "fresh client.request_id and this exact identifier."
                        ),
                    },
                    "required_intent_basis": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["user_requested", "agent_override"],
                        },
                        "uniqueItems": True,
                    },
                    "planning_intent_confirmation": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Durable first-call Planning confirmation challenge. It does not "
                            "open an operation or acquire a lease."
                        ),
                    },
                    "required_start_kind": {
                        "type": "string",
                        "enum": ["planning", "initial", "change", "verification"],
                        "description": (
                            "Exact arguments.kind for the returned start action: planning starts "
                            "Planning from a bare task; initial starts the first Research construction "
                            "after Planning; change starts a post-signoff update from an exact durably "
                            "signed ready baseline; verification starts independent Verification after Research. "
                            "A planning-to-research handoff always requires kind=initial."
                        ),
                    },
                    "required_admin_action": {
                        "type": "string",
                        "description": (
                            "Exact private administrative Action required before the agent workflow "
                            "may continue. It is not exposed in allowed_actions."
                        ),
                    },
                    "resolver": {
                        "type": "string",
                        "description": "Required private Marco resolver for an administrative continuation.",
                    },
                    "legal_next_step": {
                        "type": "string",
                        "description": (
                            "Exact legal continuation after a blocked agent mutation, including the "
                            "private resolver and the fresh follow-up Action when applicable."
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
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "current": {
                            "type": ["string", "object", "null"],
                            "description": (
                                "Exact submitted value or local context for a deterministic "
                                "document finding when one clean value exists; null otherwise. "
                                "May be a string or a structured object (e.g. adjacent header "
                                "lines) for multi-part context."
                            ),
                        }
                    },
                },
            },
        },
    }
    paths: dict[str, Any] = {}
    for spec in ACTION_COMMAND_DEFINITIONS.values():
        command = spec.name
        request_id_required = spec.request_id_required
        is_lease_command = spec.private_route == "lease"
        argument_schema = action_openapi_argument_schema(command)
        paths[f"/v1/action/{command}"] = {
            "post": {
                "operationId": f"dish_{command.replace('-', '_')}",
                "x-openai-isConsequential": request_id_required,
                "summary": (
                    "Renew the current GPT Action operation lease"
                    if is_lease_command
                    else f"Run dish {command}"
                ),
                "description": _action_operation_description(spec),
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
                                        "required": (["run_id", "request_id"] if request_id_required else ["run_id"]),
                                        "additionalProperties": False,
                                        "properties": {
                                            "run_id": dict(CLIENT_RUN_ID_SCHEMA),
                                            **(
                                                {"request_id": dict(CLIENT_REQUEST_ID_SCHEMA)}
                                                if request_id_required
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
                        "description": (
                            "Canonical lease result"
                            if is_lease_command
                            else "Canonical dish workflow result"
                        ),
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ResultEnvelope"}}},
                    }
                },
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
