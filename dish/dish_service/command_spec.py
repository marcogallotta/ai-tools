"""Shared GPT Action command contract used by HTTP validation and OpenAPI."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from dish_tool.errors import DishRuleError
from dish_tool.models import validate_actor_model, validate_independence_attestation
from .identifiers import (
    CANONICAL_DISH_UUID_SCHEMA,
    MAX_ASANA_GID_LENGTH,
    require_asana_gid,
    require_dish_uuid,
)

AGENT_MUTATION_COMMANDS = {"create", "start", "prepare", "approve", "reject", "submit"}
ACTION_LEASE_COMMAND = "renew-lease"
REPLAY_SAFE_COMMANDS = AGENT_MUTATION_COMMANDS | {ACTION_LEASE_COMMAND}
REPLAY_CAPABLE_COMMANDS = REPLAY_SAFE_COMMANDS

DISH_UUID_SCHEMA = dict(CANONICAL_DISH_UUID_SCHEMA)
ASANA_GID_SCHEMA = {
    "type": "string",
    "pattern": "^[1-9][0-9]*$",
    "maxLength": MAX_ASANA_GID_LENGTH,
}
CLIENT_RUN_ID_SCHEMA = {
    **DISH_UUID_SCHEMA,
    "description": (
        "Canonical lowercase UUID identifying this agent run. Reuse it for every "
        "call made by the same run; a new run must generate a new UUID."
    ),
}
CLIENT_REQUEST_ID_SCHEMA = {
    **DISH_UUID_SCHEMA,
    "description": (
        "Canonical lowercase UUID for one logical mutation. Dish durably binds it to the "
        "exact command, canonical arguments, authenticated owner, and client.run_id, stores "
        "the first authoritative success or expected failure, and preserves that result "
        "across service restart. Reuse it only for an exact replay after a lost response: the "
        "same identity returns the stored result with data.request_replayed=true and "
        "data.request_id. Changed arguments or reuse from a different command, owner, or run "
        "returns service_request_identity_conflict. A matching pending or uncertain request "
        "is not executed again and remains fail-closed until exact durable evidence supports "
        "reconstruction or safe resolution."
    ),
}

ACTION_COMMANDS = (
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
    ACTION_LEASE_COMMAND,
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
    "section-tasks": {
        "required": ["section_gid", "agent"],
        "properties": {
            "section_gid": dict(ASANA_GID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    "read": {
        "required": ["task_gid", "agent"],
        "properties": {
            "task_gid": dict(ASANA_GID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    "inspect": {
        "required": ["submission_id", "agent"],
        "properties": {
            "submission_id": dict(DISH_UUID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    "start": {
        "required": ["task_gid", "agent", "kind"],
        "properties": {
            "task_gid": dict(ASANA_GID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "kind": {
                "type": "string",
                "enum": ["planning", "initial", "change", "verification"],
            },
            "independence_attestation": {"type": "string"},
            "change_level": {"type": "string", "enum": ["small", "large"]},
            "change_reason": {"type": "string"},
            "prepared_operation_id": dict(DISH_UUID_SCHEMA),
            "intent_challenge_id": {
                **DISH_UUID_SCHEMA,
                "description": (
                    "Durable challenge returned by the first Planning start call. "
                    "Omit it on the first call and use it only on the fresh confirmed call."
                ),
            },
            "intent_basis": {
                "type": "string",
                "enum": ["user_requested", "agent_override"],
                "description": (
                    "Explicit basis for the fresh confirmed Planning call. "
                    "user_requested means Marco requested Planning for this exact task."
                ),
            },
            "override_reason": {
                "type": "string",
                "description": (
                    "Required non-blank explanation only when intent_basis=agent_override."
                ),
            },
            "target_operation_id": dict(DISH_UUID_SCHEMA),
            "target_cycle_id": dict(DISH_UUID_SCHEMA),
        },
    },
    "prepare": {
        "required": ["submission_id", "agent", "model", "file_text"],
        "properties": {
            "submission_id": dict(DISH_UUID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "file_text": {"type": "string"},
            "material_classification": {
                "type": "string",
                "enum": ["material", "non-material"],
                "description": (
                    "Required only when a post-signoff change candidate changes the canonical "
                    "body. It classifies that exact body diff from the signed baseline. The "
                    "caller may propose non-material, but Dish forces material when a "
                    "protocol-defined material path changed; material opens Verification, while "
                    "an accepted non-material diff preserves the exact prior signoff."
                ),
            },
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
            "submission_id": dict(DISH_UUID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "correction": {"type": "string", "enum": ["none", "small"]},
            "file_text": {"type": "string"},
            "reviewed_identity": {"type": "string"},
            "semantic_review_complete": {"type": "boolean"},
            "provenance_complete": {"type": "boolean"},
        },
    },
    "reject": {
        "required": ["submission_id", "agent", "reason", "route"],
        "properties": {
            "submission_id": dict(DISH_UUID_SCHEMA),
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
        },
    },
    "submit": {
        "required": ["submission_id"],
        "properties": {"submission_id": dict(DISH_UUID_SCHEMA)},
    },
    ACTION_LEASE_COMMAND: {
        "required": ["operation_id"],
        "properties": {"operation_id": dict(DISH_UUID_SCHEMA)},
    },
}


def action_openapi_argument_schema(command: str) -> dict[str, Any]:
    """Return the public Action schema, including route-specific shapes."""
    if command == "start":
        base = ARGUMENT_SCHEMAS["start"]["properties"]
        common = {name: deepcopy(base[name]) for name in ("task_gid", "agent")}

        start_kind_descriptions = {
            "planning": (
                "Start Planning from a bare Cooking task through the required two-call "
                "intent-confirmation gate. The first call always returns a durable challenge."
            ),
            "initial": (
                "Start the first Research construction after Planning. "
                "For a planning-to-research handoff, use kind=initial; do not start Planning again."
            ),
            "change": "Start a post-signoff change operation.",
            "verification": "Start independent Verification after Research.",
        }

        def start_variant(
            kind: str, *extras: str, required: tuple[str, ...] = ()
        ) -> dict[str, Any]:
            properties = deepcopy(common)
            properties["kind"] = {
                "type": "string",
                "const": kind,
                "description": start_kind_descriptions[kind],
            }
            for name in extras:
                properties[name] = deepcopy(base[name])
            return {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_gid", "agent", "kind", *required],
                "properties": properties,
            }

        return {
            "oneOf": [
                start_variant(
                    "planning",
                    "prepared_operation_id",
                    "intent_challenge_id",
                    "intent_basis",
                    "override_reason",
                ),
                start_variant("initial", "prepared_operation_id"),
                start_variant(
                    "change", "change_level", "change_reason",
                    "prepared_operation_id"
                ),
                start_variant(
                    "verification",
                    "independence_attestation",
                    "target_operation_id",
                    "target_cycle_id",
                    required=("independence_attestation",),
                ),
            ],
            "discriminator": {"propertyName": "kind"},
        }
    if command == "approve":
        base = ARGUMENT_SCHEMAS["approve"]["properties"]
        common_names = (
            "submission_id",
            "agent",
            "model",
            "reviewed_identity",
            "semantic_review_complete",
            "provenance_complete",
        )
        common = {name: deepcopy(base[name]) for name in common_names}

        def approve_variant(correction: str, *, with_file_text: bool) -> dict[str, Any]:
            properties = deepcopy(common)
            properties["correction"] = {
                "type": "string",
                "const": correction,
                "description": (
                    "Approve the exact inspected candidate without supplying file_text."
                    if correction == "none"
                    else "Apply and approve a complete Small corrected candidate supplied as file_text."
                ),
            }
            if with_file_text:
                properties["file_text"] = deepcopy(base["file_text"])
            required = [
                "submission_id",
                "agent",
                "model",
                "correction",
                "reviewed_identity",
                "semantic_review_complete",
                "provenance_complete",
            ]
            if with_file_text:
                required.insert(4, "file_text")
            return {
                "type": "object",
                "additionalProperties": False,
                "required": required,
                "properties": properties,
            }

        return {
            "oneOf": [
                approve_variant("none", with_file_text=False),
                approve_variant("small", with_file_text=True),
            ],
            "discriminator": {"propertyName": "correction"},
        }
    if command != "reject":
        return action_argument_schema(command)

    base = ARGUMENT_SCHEMAS["reject"]["properties"]
    common = {name: deepcopy(base[name]) for name in ("submission_id", "agent", "reason")}

    def variant(route: str, *, extra: tuple[str, ...], required: tuple[str, ...]) -> dict[str, Any]:
        properties = deepcopy(common)
        properties["route"] = {
            "type": "string",
            "const": route,
            "description": f"Select the {route} rejection route.",
        }
        for name in extra:
            properties[name] = deepcopy(base[name])
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["submission_id", "agent", "reason", "route", *required],
            "properties": properties,
        }

    return {
        "oneOf": [
            variant(
                "large",
                extra=("model", "file_text"),
                required=("model", "file_text"),
            ),
            variant(
                "evidence",
                extra=("resume_status",),
                required=("resume_status",),
            ),
            variant(
                "human-review",
                extra=("resume_status",),
                required=("resume_status",),
            ),
        ],
        "discriminator": {"propertyName": "route"},
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
    if field == "model" and isinstance(value, str):
        validate_actor_model(value)
    if field == "independence_attestation" and isinstance(value, str):
        validate_independence_attestation(value)
    if isinstance(value, str) and schema.get("format") == "uuid":
        require_dish_uuid(value, field=field)
    elif isinstance(value, str) and "pattern" in schema:
        require_asana_gid(value, field=field)


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
    client_extras = sorted(set(client) - {"run_id", "request_id"})
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
    require_dish_uuid(run_id, field="client.run_id")
    request_id = client.get("request_id")
    if command in REPLAY_SAFE_COMMANDS and (
        not isinstance(request_id, str) or not request_id.strip()
    ):
        raise _argument_error(
            "client.request_id is required for mutations",
            "request_field_required",
            field="client.request_id",
        )
    if request_id is not None and command not in REPLAY_SAFE_COMMANDS:
        raise _argument_error(
            "client.request_id is not accepted for read Actions",
            "request_field_unexpected",
            field="client.request_id",
        )
    if request_id is not None:
        if not isinstance(request_id, str):
            raise _argument_error(
                "client.request_id has the wrong type",
                "request_type_invalid",
                field="client.request_id",
            )
        require_dish_uuid(request_id, field="client.request_id")

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
    if command == "start" and arguments.get("kind") != "planning":
        for field in ("intent_challenge_id", "intent_basis", "override_reason"):
            if field in arguments:
                raise _argument_error(
                    f"{field} is accepted only for Planning starts",
                    "argument_unexpected",
                    field=field,
                )
    if (
        command == "start"
        and arguments.get("kind") != "verification"
        and "independence_attestation" in arguments
    ):
        raise _argument_error(
            "independence_attestation is accepted only for verification starts",
            "argument_unexpected",
            field="independence_attestation",
        )
    if (
        command == "start"
        and arguments.get("kind") == "verification"
        and "prepared_operation_id" in arguments
    ):
        raise _argument_error(
            "prepared_operation_id is accepted only for Planning or Research successors",
            "argument_unexpected",
            field="prepared_operation_id",
        )
    if command == "start" and arguments.get("kind") != "verification":
        for field in ("target_operation_id", "target_cycle_id"):
            if field in arguments:
                raise _argument_error(
                    f"{field} is accepted only for Verification starts",
                    "argument_unexpected",
                    field=field,
                )
    if command == "start" and arguments.get("kind") == "verification":
        has_operation = "target_operation_id" in arguments
        has_cycle = "target_cycle_id" in arguments
        if has_operation != has_cycle:
            missing = "target_cycle_id" if has_operation else "target_operation_id"
            raise _argument_error(
                "Verification target operation and cycle must be supplied together",
                "argument_required",
                field=missing,
            )
    return dict(client), dict(arguments)
