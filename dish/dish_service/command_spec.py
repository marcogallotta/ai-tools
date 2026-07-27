"""Shared GPT Action command contract used by HTTP validation and OpenAPI."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from dish_tool.errors import DishRuleError
from .identifiers import require_asana_gid, require_dish_uuid

ACTION_COMMANDS = (
    "create",
    "sections",
    "read",
    "inspect",
    "start",
    "prepare",
    "approve",
    "reject",
    "submit",
)

ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "create": {
        "required": ["agent", "title"],
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "title": {"type": "string"},
        },
    },
    "sections": {
        "required": ["agent"],
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]}
        },
    },
    "read": {
        "required": ["task_gid", "agent"],
        "properties": {
            "task_gid": {"type": "string", "pattern": "^[0-9]+$"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    "inspect": {
        "required": ["submission_id", "agent"],
        "properties": {
            "submission_id": {"type": "string", "format": "uuid"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    "start": {
        "required": ["task_gid", "agent", "kind"],
        "properties": {
            "task_gid": {"type": "string", "pattern": "^[0-9]+$"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "kind": {
                "type": "string",
                "enum": ["planning", "initial", "change", "verification"],
            },
            "run_id": {"type": "string"},
            "independence_attestation": {"type": "string"},
            "change_level": {"type": "string", "enum": ["small", "large"]},
            "change_reason": {"type": "string"},
        },
    },
    "prepare": {
        "required": ["submission_id", "agent", "model", "file_text"],
        "properties": {
            "submission_id": {"type": "string", "format": "uuid"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "file_text": {"type": "string"},
            "material_classification": {
                "type": "string",
                "enum": ["material", "non-material"],
            },
            "exemption_revision": {"type": "string"},
            "dish_name": {"type": "string"},
            "recognition": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
            "no_role_tags": {"type": "boolean"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "no_blockers": {"type": "boolean"},
        },
    },
    "approve": {
        "required": [
            "submission_id",
            "agent",
            "model",
            "correction",
            "reviewed_identity",
            "semantic_review_complete",
            "provenance_complete",
        ],
        "properties": {
            "submission_id": {"type": "string", "format": "uuid"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "correction": {"type": "string", "enum": ["none", "small"]},
            "file_text": {"type": "string"},
            "reviewed_identity": {"type": "string"},
            "semantic_review_complete": {"type": "boolean"},
            "provenance_complete": {"type": "boolean"},
            "run_id": {"type": "string"},
            "independence_attestation": {"type": "string"},
        },
    },
    "reject": {
        "required": ["submission_id", "agent", "reason", "route"],
        "properties": {
            "submission_id": {"type": "string", "format": "uuid"},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "reason": {"type": "string"},
            "route": {
                "type": "string",
                "enum": ["large", "evidence", "human-review"],
            },
            "file_text": {"type": "string"},
            "resume_status": {
                "type": "string",
                "enum": ["pending-research", "pending-verification"],
            },
            "run_id": {"type": "string"},
            "independence_attestation": {"type": "string"},
        },
    },
    "submit": {
        "required": ["submission_id"],
        "properties": {"submission_id": {"type": "string", "format": "uuid"}},
    },
}


def action_argument_schema(command: str) -> dict[str, Any]:
    try:
        schema = deepcopy(ARGUMENT_SCHEMAS[command])
    except KeyError as exc:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "command is not exposed to the GPT Action",
            rule="action_command_forbidden",
        ) from exc
    schema.update({"type": "object", "additionalProperties": False})
    return schema


def _argument_error(message: str, rule: str, *, field: str | None = None) -> DishRuleError:
    details = {} if field is None else {"field": field}
    return DishRuleError("INVALID_ARGUMENT", message, rule=rule, details=details)


def _validate_scalar(field: str, value: Any, schema: Mapping[str, Any]) -> None:
    expected = schema.get("type")
    valid = True
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "array":
        valid = isinstance(value, list)
    if not valid:
        raise _argument_error(
            f"{field} has the wrong type", "argument_type_invalid", field=field
        )
    if expected == "array":
        item_schema = schema.get("items") or {}
        for item in value:
            if item_schema.get("type") == "string" and not isinstance(item, str):
                raise _argument_error(
                    f"{field} contains an invalid item",
                    "argument_item_type_invalid",
                    field=field,
                )
    if "enum" in schema and value not in schema["enum"]:
        raise _argument_error(
            f"{field} has an unsupported value", "argument_value_invalid", field=field
        )
    if isinstance(value, str) and "pattern" in schema:
        require_asana_gid(value, field=field)
    if isinstance(value, str) and schema.get("format") == "uuid":
        require_dish_uuid(value, field=field)


def validate_action_request(command: str, request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed_top = {"client", "arguments"}
    extras = sorted(set(request) - allowed_top)
    if extras:
        raise _argument_error(
            "request contains an unexpected field",
            "request_field_unexpected",
            field=extras[0],
        )
    if "client" not in request:
        raise _argument_error("client is required", "request_field_required", field="client")
    if "arguments" not in request:
        raise _argument_error(
            "arguments are required", "request_field_required", field="arguments"
        )

    client = request["client"]
    if not isinstance(client, dict):
        raise _argument_error("client must be an object", "request_type_invalid", field="client")
    client_extras = sorted(set(client) - {"run_id"})
    if client_extras:
        raise _argument_error(
            "client contains an unexpected field",
            "request_field_unexpected",
            field=f"client.{client_extras[0]}",
        )
    run_id = client.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise _argument_error(
            "client.run_id is required",
            "request_field_required",
            field="client.run_id",
        )

    arguments = request["arguments"]
    if not isinstance(arguments, dict):
        raise _argument_error(
            "arguments must be an object", "arguments_object_required", field="arguments"
        )
    schema = action_argument_schema(command)
    properties = schema["properties"]
    missing = [field for field in schema.get("required", []) if field not in arguments]
    if missing:
        raise _argument_error(
            f"{missing[0]} is required", "argument_required", field=missing[0]
        )
    extras = sorted(set(arguments) - set(properties))
    if extras:
        raise _argument_error(
            f"{extras[0]} is not accepted", "argument_unexpected", field=extras[0]
        )
    for field, value in arguments.items():
        _validate_scalar(field, value, properties[field])
    return dict(client), dict(arguments)
