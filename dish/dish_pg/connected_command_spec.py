"""Transport-neutral contract for Dish connected-agent commands.

This registry owns the ordinary 18-command connected surface used by MCP and,
during migration, by transport adapters. PostgreSQL remains workflow/replay
authority; this module describes and validates the connected contract only.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from dish_tool.errors import DishRuleError
from dish_tool.identifiers import CANONICAL_DISH_UUID_SCHEMA

from .command_contract import (
    ACTION_COMMANDS as _POSTGRES_CONNECTED_COMMANDS,
    COMMAND_DEFINITIONS,
    POSTGRES_CLIENT_REQUEST_ID_SCHEMA,
    POSTGRES_CLIENT_RUN_ID_SCHEMA,
    connected_argument_schema,
    validate_connected_request,
)

ConnectedPrincipal = Literal["reader", "agent", "verification"]
ConnectedKind = Literal["read", "mutation", "continuation"]


@dataclass(frozen=True, slots=True)
class ConnectedCommandSpec:
    """Canonical metadata for one ordinary connected-agent command."""

    name: str
    principal: ConnectedPrincipal
    request_replay: bool
    kind: ConnectedKind
    workflow_action: str | None
    title: str
    description: str
    read_only: bool
    destructive: bool
    open_world: bool
    idempotent: bool

    @property
    def tool_name(self) -> str:
        return f"dish_{self.name.replace('-', '_')}"

    def argument_schema(self) -> dict[str, Any]:
        return connected_argument_schema(self.name)

    def input_schema(self) -> dict[str, Any]:
        client_properties: dict[str, Any] = {
            "run_id": deepcopy(POSTGRES_CLIENT_RUN_ID_SCHEMA),
        }
        client_required = ["run_id"]
        if self.request_replay:
            client_properties["request_id"] = deepcopy(POSTGRES_CLIENT_REQUEST_ID_SCHEMA)
            client_required.append("request_id")
        return {
            "type": "object",
            "required": ["client", "arguments"],
            "additionalProperties": False,
            "properties": {
                "client": {
                    "type": "object",
                    "required": client_required,
                    "additionalProperties": False,
                    "properties": client_properties,
                },
                "arguments": self.argument_schema(),
            },
        }

    def annotations(self) -> dict[str, bool]:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "openWorldHint": self.open_world,
            "idempotentHint": self.idempotent,
        }

    def validate(
        self, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return validate_connected_request(self.name, request)


_READ_DESCRIPTIONS = {
    "sections": "List current Dish sections from PostgreSQL authority.",
    "section-tasks": "List current Dishes in one section using exact returned identifiers.",
    "search": "Search current active Dish titles through PostgreSQL authority.",
    "cook-logs": "List immutable cook logs for one Dish.",
    "read": "Read one exact Dish by canonical Dish ID or returned task identity.",
    "proposals": "List current governed proposals visible to the connected agent.",
}


def _kind(name: str) -> ConnectedKind:
    definition = COMMAND_DEFINITIONS[name]
    if not definition.request_replay:
        return "read"
    if definition.operation_required:
        return "continuation"
    return "mutation"


def _description(name: str) -> str:
    definition = COMMAND_DEFINITIONS[name]
    if name in _READ_DESCRIPTIONS:
        return _READ_DESCRIPTIONS[name]
    if definition.description:
        return definition.description
    if definition.workflow_action:
        return (
            f"Execute the governed {definition.workflow_action} continuation "
            "through PostgreSQL authority."
        )
    return f"Execute the governed Dish {name} command through PostgreSQL authority."


def _spec(name: str) -> ConnectedCommandSpec:
    definition = COMMAND_DEFINITIONS[name]
    if definition.principal not in {"reader", "agent", "verification"}:
        raise ValueError(f"connected command {name!r} has invalid principal")
    kind = _kind(name)
    return ConnectedCommandSpec(
        name=name,
        principal=definition.principal,
        request_replay=definition.request_replay,
        kind=kind,
        workflow_action=definition.workflow_action,
        title=f"Dish {name}",
        description=_description(name),
        read_only=kind == "read",
        # Preserve the qualified quick-MCP safety hint for replay-bound writes.
        destructive=definition.request_replay,
        open_world=False,
        # Exact Dish request IDs make admitted mutation replays idempotent.
        idempotent=True,
    )


CONNECTED_COMMANDS = tuple(_POSTGRES_CONNECTED_COMMANDS)
CONNECTED_COMMAND_SPECS = tuple(_spec(name) for name in CONNECTED_COMMANDS)
CONNECTED_COMMAND_DEFINITIONS = {spec.name: spec for spec in CONNECTED_COMMAND_SPECS}
TOOL_COMMANDS = {spec.tool_name: spec.name for spec in CONNECTED_COMMAND_SPECS}

if len(CONNECTED_COMMANDS) != 18:
    raise ValueError("connected-agent inventory must remain exactly 18 commands")
if len(TOOL_COMMANDS) != len(CONNECTED_COMMANDS):
    raise ValueError("connected-agent MCP tool names are not unique")


def result_envelope_schema(*, command: str | None = None) -> dict[str, Any]:
    """Return the canonical connected Dish ResultEnvelope schema.

    During the additive migration this intentionally preserves the qualified
    PostgreSQL Action output schema exactly, including the Create refinement.
    The neutral registry owns the shape so the native MCP path does not import
    or execute the Action/OpenAPI transport.
    """

    if command is not None and command not in CONNECTED_COMMAND_DEFINITIONS:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "command is not exposed to connected agents",
            rule="connected_command_forbidden",
            details={"command": command},
        )
    base: dict[str, Any] = {
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
            "command": {"type": "string", "enum": list(CONNECTED_COMMANDS)},
            "code": {"type": "string"},
            "http_status": {"type": "integer"},
            "task_gid": {"type": ["string", "null"]},
            "submission_id": {
                **deepcopy(CANONICAL_DISH_UUID_SCHEMA),
                "type": ["string", "null"],
            },
            "state": {"type": ["string", "null"]},
            "retryable": {"type": "boolean"},
            "request_replayed": {"type": "boolean"},
            "allowed_actions": {
                "type": "array",
                "items": {"type": "string", "enum": list(CONNECTED_COMMANDS)},
            },
            "data": {"type": "object", "additionalProperties": True},
            "errors": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
        },
    }
    if command != "create":
        return base
    return {
        "allOf": [
            base,
            {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "required": ["dish_id"],
                        "properties": {
                            "dish_id": deepcopy(CANONICAL_DISH_UUID_SCHEMA)
                        },
                        "additionalProperties": True,
                    }
                },
            },
        ],
        "type": "object",
    }


def definition_for(command: str) -> ConnectedCommandSpec:
    try:
        return CONNECTED_COMMAND_DEFINITIONS[command]
    except KeyError as exc:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "command is not exposed to connected agents",
            rule="connected_command_forbidden",
            details={"command": command},
        ) from exc
