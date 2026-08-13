"""Approved Stage A PostgreSQL command semantic contract.

This is executable metadata for the isolated target. It follows the command/surface architecture
and deliberately contains no transport-owned workflow rules.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from dish_service.command_spec import (
    APPROVE_COMMAND,
    CREATE_COMMAND,
    INSPECT_COMMAND,
    PREPARE_COMMAND,
    READ_COMMAND,
    REJECT_COMMAND,
    RENEW_LEASE_COMMAND,
    SECTIONS_COMMAND,
    SECTION_TASKS_COMMAND,
    START_COMMAND,
    SUBMIT_COMMAND,
    ActionCommandSpec,
    ACTION_COMMANDS as CONNECTED_ACTION_COMMANDS,
    CLIENT_REQUEST_ID_SCHEMA,
    CLIENT_RUN_ID_SCHEMA,
    action_openapi_argument_schema,
    validate_action_request as validate_legacy_action_request,
)
from dish_tool.errors import DishRuleError
from dish_tool.identifiers import CANONICAL_DISH_UUID_SCHEMA, require_dish_uuid

Profile = Literal["Q", "E", "L", "R", "P", "X"]
Principal = Literal["reader", "agent", "verification", "admin", "historical"]


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    profile: Profile
    principal: Principal
    request_replay: bool
    task_required: bool
    operation_required: bool
    retained: bool = True
    action_exposed: bool = False
    description: str = ""
    workflow_action: str | None = None


def _current_action(
    current: ActionCommandSpec,
    profile: Profile,
    *,
    task_required: bool,
    operation_required: bool,
) -> CommandDefinition:
    """Project current Action identity/replay policy into the PG target metadata."""

    return CommandDefinition(
        name=current.name,
        profile=profile,
        principal=current.principal,
        request_replay=current.request_id_required,
        task_required=task_required,
        operation_required=operation_required,
        action_exposed=True,
        workflow_action=current.workflow_action,
    )


COMMAND_DEFINITIONS = {
    row.name: row
    for row in (
        _current_action(CREATE_COMMAND, "L", task_required=False, operation_required=False),
        _current_action(SECTIONS_COMMAND, "Q", task_required=False, operation_required=False),
        _current_action(SECTION_TASKS_COMMAND, "Q", task_required=False, operation_required=False),
        _current_action(READ_COMMAND, "Q", task_required=True, operation_required=False),
        CommandDefinition("attention", "Q", "admin", False, False, False),
        CommandDefinition("holds", "Q", "admin", False, False, False),
        _current_action(INSPECT_COMMAND, "E", task_required=True, operation_required=True),
        _current_action(START_COMMAND, "L", task_required=True, operation_required=False),
        _current_action(PREPARE_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(APPROVE_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(REJECT_COMMAND, "L", task_required=True, operation_required=True),
        CommandDefinition(
            "hold-reject",
            "L",
            "agent",
            True,
            True,
            True,
            action_exposed=False,
            workflow_action=None,
        ),
        _current_action(SUBMIT_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(RENEW_LEASE_COMMAND, "L", task_required=True, operation_required=True),
        CommandDefinition("recover", "P", "admin", True, True, False),
        CommandDefinition("repair-destination", "P", "admin", True, True, False),
        CommandDefinition("discard", "R", "admin", True, True, True),
        CommandDefinition("abandon-operation", "R", "admin", True, True, True),
        CommandDefinition("reconcile-abandonment", "R", "admin", True, True, True),
        CommandDefinition("reopen-planning", "L", "admin", True, True, False),
        CommandDefinition("reopen", "R", "admin", True, True, True, workflow_action="reopen"),
        CommandDefinition("supply-evidence", "R", "admin", True, True, True, workflow_action="supply-evidence"),
        CommandDefinition("record-human-decision", "R", "admin", True, True, True, workflow_action="record-human-decision"),
        CommandDefinition("resolved", "R", "admin", True, True, True, workflow_action="resolved"),
        CommandDefinition("authorize-governed-change", "L", "admin", True, True, False),
        CommandDefinition("revise-section-registry", "L", "admin", True, False, False),
        CommandDefinition("recover-lease", "R", "admin", True, True, False),
        CommandDefinition("expire-lease", "L", "admin", True, True, False),
        CommandDefinition("migrate", "L", "admin", True, True, False),
        CommandDefinition("planning-intent-settlement", "L", "admin", True, True, False),
        CommandDefinition("backup-create", "X", "historical", False, False, False, retained=False),
        CommandDefinition("backup-restore", "X", "historical", False, False, False, retained=False),
    )
}

ACTION_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if definition.action_exposed
)
ADMIN_COMMANDS = tuple(
    name
    for name, definition in COMMAND_DEFINITIONS.items()
    if definition.principal == "admin" or definition.principal == "historical"
)
RETAINED_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if definition.retained
)
RETIRED_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if not definition.retained
)

# These commands remain part of the legacy connected-agent product but are deliberately
# not part of the retained no-Asana PostgreSQL Action contract.  Keeping the disposition
# executable prevents a future reader from mistaking omission for unfinished parity.
CONNECTED_COMMAND_DISPOSITIONS: dict[str, str] = {
    command: ("retained" if command in ACTION_COMMANDS else "retired-from-postgresql-action")
    for command in CONNECTED_ACTION_COMMANDS
}
POSTGRESQL_ACTION_RETIRED_COMMANDS = tuple(
    command
    for command, disposition in CONNECTED_COMMAND_DISPOSITIONS.items()
    if disposition == "retired-from-postgresql-action"
)
if POSTGRESQL_ACTION_RETIRED_COMMANDS != (
    "proposals",
    "apply-proposal",
    "safe-reclaim",
):
    raise ValueError("PostgreSQL connected-command retirement inventory drifted")


def _add_canonical_identity_alias(
    schema: Mapping[str, Any],
    *,
    legacy_field: str,
    canonical_field: str,
) -> dict[str, Any]:
    """Make a canonical UUID primary while retaining one exact local alias alternative."""

    result = deepcopy(dict(schema))
    properties = result.get("properties")
    required = result.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("identity alias adaptation requires an object schema")
    if legacy_field not in properties or legacy_field not in required:
        raise ValueError(f"schema does not require {legacy_field}")
    properties[canonical_field] = dict(CANONICAL_DISH_UUID_SCHEMA)
    result["required"] = [field for field in required if field != legacy_field]
    result["oneOf"] = [
        {"required": [canonical_field]},
        {"required": [legacy_field]},
    ]
    return result


def postgres_action_argument_schema(command: str) -> dict[str, Any]:
    """Return the no-Asana PostgreSQL Action argument schema.

    Canonical Dish/section UUIDs are the primary external identities.  Exact legacy
    Asana GIDs remain schema-visible only as optional compatibility alternatives and
    resolve exclusively through PostgreSQL alias rows.
    """

    base = action_openapi_argument_schema(command)
    if command == "section-tasks":
        return _add_canonical_identity_alias(
            base,
            legacy_field="section_gid",
            canonical_field="section_id",
        )
    if command == "start":
        variants = base.get("oneOf")
        if not isinstance(variants, list):
            raise ValueError("start Action schema is not variant-based")
        adapted_variants = [
            _add_canonical_identity_alias(
                variant, legacy_field="task_gid", canonical_field="dish_id"
            )
            for variant in variants
        ]
        return {
            "oneOf": adapted_variants,
            **(
                {"discriminator": deepcopy(base["discriminator"])}
                if "discriminator" in base
                else {}
            ),
        }
    return deepcopy(base)


def validate_postgres_action_request(
    command: str, request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate PostgreSQL Action requests without requiring Asana identities.

    Existing shared command-specific validation remains the semantic oracle.  For the
    two legacy fields that differ at the PostgreSQL boundary, canonical UUIDs are
    validated locally then represented to the legacy validator by a harmless synthetic
    GID.  The returned arguments preserve the caller's canonical identity unchanged.
    """

    if command not in ACTION_COMMANDS:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "command is not retained by the PostgreSQL Action contract",
            rule="action_command_forbidden",
        )
    if not isinstance(request, Mapping):
        return validate_legacy_action_request(command, request)
    raw_arguments = request.get("arguments")
    if not isinstance(raw_arguments, Mapping):
        return validate_legacy_action_request(command, request)
    arguments = dict(raw_arguments)
    adapted = dict(request)
    adapted_arguments = dict(arguments)

    identity_pair: tuple[str, str] | None = None
    if command == "section-tasks":
        identity_pair = ("section_id", "section_gid")
    elif command == "start":
        identity_pair = ("dish_id", "task_gid")

    if identity_pair is not None:
        canonical_field, legacy_field = identity_pair
        has_canonical = canonical_field in arguments
        has_legacy = legacy_field in arguments
        if has_canonical and has_legacy:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"exactly one of {canonical_field} and {legacy_field} is allowed",
                rule="argument_identity_conflict",
                details={"fields": [canonical_field, legacy_field]},
            )
        if has_canonical:
            require_dish_uuid(arguments[canonical_field], field=canonical_field)
            adapted_arguments.pop(canonical_field, None)
            adapted_arguments[legacy_field] = "1"
            adapted["arguments"] = adapted_arguments

    client, validated_arguments = validate_legacy_action_request(command, adapted)
    if identity_pair is not None and identity_pair[0] in arguments:
        canonical_field, legacy_field = identity_pair
        validated_arguments.pop(legacy_field, None)
        validated_arguments[canonical_field] = arguments[canonical_field]
    return client, validated_arguments


POSTGRES_CLIENT_RUN_ID_SCHEMA = deepcopy(CLIENT_RUN_ID_SCHEMA)
POSTGRES_CLIENT_REQUEST_ID_SCHEMA = deepcopy(CLIENT_REQUEST_ID_SCHEMA)
POSTGRES_DISH_ID_SCHEMA = dict(CANONICAL_DISH_UUID_SCHEMA)


def definition_for(command_name: str) -> CommandDefinition:
    try:
        return COMMAND_DEFINITIONS[command_name]
    except KeyError as exc:
        raise ValueError(f"unknown PostgreSQL command: {command_name}") from exc
