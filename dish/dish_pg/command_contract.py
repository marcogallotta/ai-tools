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
    PROPOSALS_COMMAND,
    APPLY_PROPOSAL_COMMAND,
    QUALIFY_FILE_TRANSPORT_COMMAND,
    SAFE_RECLAIM_COMMAND,
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

SEARCH_COMMAND = "search"
SEARCH_QUERY_MAX_LENGTH = 160
SEARCH_PAGE_SIZE_DEFAULT = 50
SEARCH_PAGE_SIZE_MAX = 100
_SEARCH_AGENT_VALUES = ("claude", "gpt", "codex")


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
    admin_exposed: bool = False


def _current_action(
    current: ActionCommandSpec,
    profile: Profile,
    *,
    task_required: bool,
    operation_required: bool,
    admin_exposed: bool = False,
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
        admin_exposed=admin_exposed,
    )


COMMAND_DEFINITIONS = {
    row.name: row
    for row in (
        _current_action(CREATE_COMMAND, "L", task_required=False, operation_required=False),
        _current_action(SECTIONS_COMMAND, "Q", task_required=False, operation_required=False),
        _current_action(SECTION_TASKS_COMMAND, "Q", task_required=False, operation_required=False),
        CommandDefinition(
            SEARCH_COMMAND,
            "Q",
            "reader",
            False,
            False,
            False,
            action_exposed=True,
            description="Search current active Dish titles through canonical PostgreSQL authority.",
        ),
        _current_action(READ_COMMAND, "Q", task_required=True, operation_required=False),
        _current_action(PROPOSALS_COMMAND, "Q", task_required=False, operation_required=False),
        _current_action(APPLY_PROPOSAL_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(SAFE_RECLAIM_COMMAND, "R", task_required=True, operation_required=True),
        CommandDefinition("attention", "Q", "admin", False, False, False),
        CommandDefinition("holds", "Q", "admin", False, False, False),
        _current_action(
            INSPECT_COMMAND,
            "E",
            task_required=True,
            operation_required=True,
            admin_exposed=True,
        ),
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
        CommandDefinition("cooked", "L", "agent", True, True, False),
        CommandDefinition(
            "archive", "L", "agent", True, True, False, admin_exposed=True
        ),
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
    if definition.principal in {"admin", "historical"} or definition.admin_exposed
)
RETAINED_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if definition.retained
)
RETIRED_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if not definition.retained
)

# Every previously connected command remains retained. Search is the one intentional
# PostgreSQL-native addition for pre-cutover active-title discovery.
CONNECTED_COMMAND_DISPOSITIONS: dict[str, str] = {
    command: "retained" for command in ACTION_COMMANDS
}
POSTGRESQL_ACTION_ADDED_COMMANDS: tuple[str, ...] = (SEARCH_COMMAND,)
POSTGRESQL_ACTION_RETIRED_COMMANDS: tuple[str, ...] = ()
# Connected on the legacy SQLite/Asana Action surface but not yet ported to the
# PostgreSQL command-execution stack: no PG workflow/transaction handler exists
# for these. Each entry here must also be a "source_only_commands" entry in
# docs/database-backend-stage-a-baseline.json, never a claimed "retain"/"add"
# treatment, so cutover-readiness evidence stays honest.
CONNECTED_ACTION_COMMANDS_NOT_YET_PORTED: tuple[str, ...] = (
    QUALIFY_FILE_TRANSPORT_COMMAND.name,
)
_PARITY_EXPECTED_CONNECTED_COMMANDS = tuple(
    command
    for command in CONNECTED_ACTION_COMMANDS
    if command not in CONNECTED_ACTION_COMMANDS_NOT_YET_PORTED
)
if tuple(ACTION_COMMANDS) != (
    *tuple(_PARITY_EXPECTED_CONNECTED_COMMANDS[:3]),
    SEARCH_COMMAND,
    *tuple(_PARITY_EXPECTED_CONNECTED_COMMANDS[3:]),
):
    raise ValueError("PostgreSQL connected-command inventory drifted")


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


def _search_argument_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["query", "agent"],
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": SEARCH_QUERY_MAX_LENGTH,
                "description": (
                    "Case-insensitive literal substring matched only against the current "
                    "canonical active Dish title."
                ),
            },
            "agent": {"type": "string", "enum": list(_SEARCH_AGENT_VALUES)},
            "cursor": {
                "type": "string",
                "description": (
                    "Opaque next_cursor returned by a prior search page. Omit for the first "
                    "page; a null next_cursor means the result set is exhausted."
                ),
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": SEARCH_PAGE_SIZE_MAX,
                "default": SEARCH_PAGE_SIZE_DEFAULT,
            },
        },
    }


def postgres_action_argument_schema(command: str) -> dict[str, Any]:
    """Return the no-Asana PostgreSQL Action argument schema.

    Canonical Dish/Section UUIDs are the PostgreSQL-native identities. Legacy
    section GIDs are not accepted at this boundary.
    """

    if command == SEARCH_COMMAND:
        return _search_argument_schema()
    base = action_openapi_argument_schema(command)
    if command == "section-tasks":
        schema = deepcopy(base)
        properties = schema["properties"]
        properties.pop("section_gid", None)
        properties["section_id"] = deepcopy(POSTGRES_DISH_ID_SCHEMA)
        schema["required"] = [
            "section_id" if field == "section_gid" else field
            for field in schema.get("required", [])
        ]
        schema.pop("oneOf", None)
        return schema
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


def normalize_postgres_search_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the one PostgreSQL Search argument contract for every transport."""

    if not isinstance(arguments, Mapping):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "arguments must be an object",
            rule="argument_object_required",
        )
    allowed = {"query", "agent", "cursor", "page_size"}
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "search arguments contain unsupported fields",
            rule="argument_field_forbidden",
            details={"fields": unknown},
        )
    agent = arguments.get("agent")
    if not isinstance(agent, str) or agent not in _SEARCH_AGENT_VALUES:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "agent must name a supported agent family",
            rule="argument_value_invalid",
            details={"field": "agent", "allowed": list(_SEARCH_AGENT_VALUES)},
        )
    query = arguments.get("query")
    if not isinstance(query, str):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "query must be a string",
            rule="argument_type_invalid",
            details={"field": "query"},
        )
    clean_query = query.strip()
    if not clean_query:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "query is required",
            rule="search_query_required",
            details={"field": "query"},
        )
    if len(clean_query) > SEARCH_QUERY_MAX_LENGTH:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "query is too long",
            rule="search_query_too_long",
            details={"field": "query", "maximum": SEARCH_QUERY_MAX_LENGTH},
        )
    cursor = arguments.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "cursor must be a non-empty string when provided",
            rule="argument_type_invalid",
            details={"field": "cursor"},
        )
    page_size = arguments.get("page_size", SEARCH_PAGE_SIZE_DEFAULT)
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "page_size must be an integer",
            rule="argument_type_invalid",
            details={"field": "page_size"},
        )
    if not 1 <= page_size <= SEARCH_PAGE_SIZE_MAX:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "page_size is outside the supported range",
            rule="argument_range_invalid",
            details={"field": "page_size", "minimum": 1, "maximum": SEARCH_PAGE_SIZE_MAX},
        )
    return {
        "query": clean_query,
        "agent": agent,
        "page_size": page_size,
        **({"cursor": cursor} if cursor is not None else {}),
    }


def _validate_search_action_request(
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_arguments = request.get("arguments") if isinstance(request, Mapping) else None
    adapted = dict(request) if isinstance(request, Mapping) else request
    if isinstance(adapted, dict):
        # Reuse the settled Action client-envelope validator without making it
        # a second authority for Search's argument semantics.
        adapted["arguments"] = {"agent": "gpt"}
    client, _ = validate_legacy_action_request(SECTIONS_COMMAND.name, adapted)
    return client, normalize_postgres_search_arguments(raw_arguments)


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
    if command == SEARCH_COMMAND:
        return _validate_search_action_request(request)
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
        if "section_gid" in arguments:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "section_gid is transition-only; use canonical section_id",
                rule="native_section_id_required",
                details={"field": "section_id"},
            )
        if "section_id" not in arguments:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "section_id is required",
                rule="native_section_id_required",
                details={"field": "section_id"},
            )
        require_dish_uuid(arguments["section_id"], field="section_id")
        adapted_arguments.pop("section_id", None)
        adapted_arguments["section_gid"] = "1"
        adapted["arguments"] = adapted_arguments
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
