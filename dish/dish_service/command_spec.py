"""Shared GPT Action command contract used by HTTP validation and OpenAPI."""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from dish_tool import command_identity as command_ids
from dish_tool.constants import APPROVAL_CORRECTIONS, REJECTION_ROUTES
from dish_tool.errors import DishRuleError
from dish_tool.identifiers import (
    CANONICAL_DISH_UUID_SCHEMA,
    MAX_ASANA_GID_LENGTH,
    require_asana_gid,
    require_dish_uuid,
)
from dish_tool.models import validate_actor_model, validate_independence_attestation

from .file_transport import MAX_FETCH_BYTES as QUALIFY_FILE_TRANSPORT_MAX_BYTES

ActionPrincipal = Literal["reader", "agent", "verification"]
ActionRoute = Literal["agent", "lease"]


@dataclass(frozen=True)
class ActionCommandSpec:
    """Descriptive policy for one command exposed by the GPT Action surface."""

    name: str
    principal: ActionPrincipal
    request_id_required: bool
    private_route: ActionRoute = "agent"
    workflow_action: str | None = None

def _action(
    name: str,
    principal: ActionPrincipal,
    *,
    request_id: bool,
    private_route: ActionRoute = "agent",
    workflow_action: str | None = None,
) -> ActionCommandSpec:
    return ActionCommandSpec(
        name=name,
        principal=principal,
        request_id_required=request_id,
        private_route=private_route,
        workflow_action=workflow_action,
    )


CREATE_COMMAND = _action(command_ids.CREATE, "agent", request_id=True)
SECTIONS_COMMAND = _action(command_ids.SECTIONS, "reader", request_id=False)
SECTION_TASKS_COMMAND = _action(command_ids.SECTION_TASKS, "reader", request_id=False)
READ_COMMAND = _action(command_ids.READ, "reader", request_id=False)
PROPOSALS_COMMAND = _action(command_ids.PROPOSALS, "reader", request_id=False)
APPLY_PROPOSAL_COMMAND = _action(
    command_ids.APPLY_PROPOSAL, "agent", request_id=True, workflow_action="apply-proposal"
)
SAFE_RECLAIM_COMMAND = _action(command_ids.SAFE_RECLAIM, "agent", request_id=True)
INSPECT_COMMAND = _action(command_ids.INSPECT, "verification", request_id=True)
START_COMMAND = _action(command_ids.START, "agent", request_id=True)
PREPARE_COMMAND = _action(
    command_ids.PREPARE, "agent", request_id=True, workflow_action="prepare"
)
APPROVE_COMMAND = _action(
    command_ids.APPROVE, "verification", request_id=True, workflow_action="approve"
)
REJECT_COMMAND = _action(
    command_ids.REJECT, "verification", request_id=True, workflow_action="reject"
)
SUBMIT_COMMAND = _action(
    command_ids.SUBMIT, "agent", request_id=True, workflow_action="submit"
)
RENEW_LEASE_COMMAND = _action(
    command_ids.RENEW_LEASE, "agent", request_id=True, private_route="lease"
)
QUALIFY_FILE_TRANSPORT_COMMAND = _action(
    command_ids.QUALIFY_FILE_TRANSPORT, "agent", request_id=True
)

ACTION_COMMAND_SPECS = (
    CREATE_COMMAND,
    SECTIONS_COMMAND,
    SECTION_TASKS_COMMAND,
    READ_COMMAND,
    PROPOSALS_COMMAND,
    APPLY_PROPOSAL_COMMAND,
    SAFE_RECLAIM_COMMAND,
    INSPECT_COMMAND,
    START_COMMAND,
    PREPARE_COMMAND,
    APPROVE_COMMAND,
    REJECT_COMMAND,
    SUBMIT_COMMAND,
    RENEW_LEASE_COMMAND,
    QUALIFY_FILE_TRANSPORT_COMMAND,
)
ACTION_COMMAND_DEFINITIONS = {spec.name: spec for spec in ACTION_COMMAND_SPECS}
if len(ACTION_COMMAND_DEFINITIONS) != len(ACTION_COMMAND_SPECS):
    raise ValueError("duplicate GPT Action command definition")

ACTION_COMMANDS = command_ids.CONNECTED_AGENT_COMMANDS
if ACTION_COMMANDS != tuple(spec.name for spec in ACTION_COMMAND_SPECS):
    raise ValueError("GPT Action command metadata does not cover connected-agent identities")
ACTION_LEASE_COMMAND = RENEW_LEASE_COMMAND.name
ACTION_QUALIFY_FILE_TRANSPORT_COMMAND = QUALIFY_FILE_TRANSPORT_COMMAND.name
QUALIFY_FILE_TRANSPORT_FILE_FIELDS = ("id", "name", "mime_type", "download_link")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
REPLAY_SAFE_COMMANDS = frozenset(
    spec.name for spec in ACTION_COMMAND_SPECS if spec.request_id_required
)

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

ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    CREATE_COMMAND.name: {
        "required": ["agent", "title"],
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "title": {"type": "string"},
        },
    },
    SECTIONS_COMMAND.name: {
        "required": ["agent"],
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]}
        },
    },
    SECTION_TASKS_COMMAND.name: {
        "required": ["section_gid", "agent"],
        "properties": {
            "section_gid": dict(ASANA_GID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "cursor": {
                "type": "string",
                "description": (
                    "Opaque next_cursor returned by a prior section-tasks call. Omit for the "
                    "first page; repeat the call with the returned next_cursor to fetch the "
                    "next page. A null next_cursor means there are no more tasks."
                ),
            },
        },
    },
    READ_COMMAND.name: {
        "required": ["agent"],
        "properties": {
            "task_gid": {**dict(ASANA_GID_SCHEMA), "description": "Exact task GID. Use this only when already returned by Dish."},
            "dish_id": {**dict(DISH_UUID_SCHEMA), "description": "Canonical Dish UUID. Use this to resolve a Marco-supplied `dish <uuid>` handoff deterministically; never rediscover that Dish by browsing sections or matching titles."},
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    INSPECT_COMMAND.name: {
        "required": ["submission_id", "agent"],
        "properties": {
            "submission_id": dict(DISH_UUID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    PROPOSALS_COMMAND.name: {
        "required": ["agent"],
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    APPLY_PROPOSAL_COMMAND.name: {
        "required": ["proposal_id", "agent", "model"],
        "properties": {
            "proposal_id": dict(DISH_UUID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
        },
    },
    SAFE_RECLAIM_COMMAND.name: {
        "required": ["submission_id", "lease_id", "agent"],
        "properties": {
            "submission_id": dict(DISH_UUID_SCHEMA),
            "lease_id": dict(DISH_UUID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
        },
    },
    START_COMMAND.name: {
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
            "target_operation_id": {
                **DISH_UUID_SCHEMA,
                "description": (
                    "Omit for an ordinary Verification start. Supply only as part of the exact "
                    "operation/cycle pair returned by Dish for an abandonment continuation; "
                    "never copy submission_id into this field."
                ),
            },
            "target_cycle_id": {
                **DISH_UUID_SCHEMA,
                "description": (
                    "Omit for an ordinary Verification start. Supply only as part of the exact "
                    "operation/cycle pair returned by Dish for an abandonment continuation; "
                    "never invent or infer this value."
                ),
            },
        },
    },
    PREPARE_COMMAND.name: {
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
            "governed_change_fields": {
                "type": "array",
                "items": {"type": "string", "enum": ["Decisions"]},
                "uniqueItems": True,
                "description": (
                    "Use only to explicitly attest an append-only `Human — Marco:` Decision "
                    "that Marco actually stated in conversation. This records agent-attributed "
                    "provenance; it does not authorize any other governed-field mutation."
                ),
            },
        },
    },
    APPROVE_COMMAND.name: {
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
            "correction": {"type": "string", "enum": list(APPROVAL_CORRECTIONS)},
            "file_text": {"type": "string"},
            "reviewed_identity": {"type": "string"},
            "semantic_review_complete": {"type": "boolean"},
            "provenance_complete": {"type": "boolean"},
        },
    },
    REJECT_COMMAND.name: {
        "required": ["submission_id", "agent", "reason", "route"],
        "properties": {
            "submission_id": dict(DISH_UUID_SCHEMA),
            "agent": {"type": "string", "enum": ["claude", "gpt", "codex"]},
            "model": {"type": "string"},
            "reason": {"type": "string"},
            "route": {
                "type": "string",
                "enum": list(REJECTION_ROUTES),
            },
            "file_text": {"type": "string"},
            "blocker_metric": {"type": "string"},
            "blocker_actual": {"type": "number"},
            "blocker_limit": {"type": "number"},
            "blocker_delta": {"type": "number"},
            "blocker_unit": {"type": "string"},
            "blocker_basis": {"type": "string"},
            "human_review_confirmed": {
                "type": "boolean",
                "description": "Set true only after completing Dish's Human Review escalation preflight.",
            },
            "human_review_basis": {
                "type": "string",
                "description": "The specific unresolved choice, waiver, classification, or authority that remains for Marco.",
            },
            "repairs_considered": {
                "type": "string",
                "description": "Specific plausible repairs considered and why they do not resolve the issue within existing authority.",
            },
            "human_review_options": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "description": (
                    "Concrete human choices for this Human Review, ordered best-first. The first "
                    "item is always the agent's recommended route. Include plausible alternatives; "
                    "Dish always gives Marco a free-text Other choice in the admin UI. If an option "
                    "authorizes an Exemptions change, before/after must be the complete exact field "
                    "values. Nutrition waivers use the canonical tags [nutrition-kcal], "
                    "[nutrition-protein], or [nutrition-fat] in the after value, not prose substitutes."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "decision"],
                    "properties": {
                        "label": {"type": "string"},
                        "decision": {"type": "string"},
                        "authorization": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["field", "before", "after"],
                            "properties": {
                                "field": {
                                    "type": "string",
                                    "enum": [
                                        "Dish candidate", "Purpose", "Role", "Priors", "Locks",
                                        "Exemptions", "Research emphasis", "Destination section",
                                        "Researched by",
                                    ],
                                },
                                "before": {"type": "string"},
                                "after": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "governed_change_fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["Dish candidate", "Purpose", "Role", "Priors", "Locks", "Exemptions", "Research emphasis", "Destination section", "Decisions", "Researched by"],
                },
                "uniqueItems": True,
                "description": (
                    "On a Large-correction retry, name only governed fields Dish asked the agent "
                    "to confirm as intentionally changed. Naming Decisions also attests that an "
                    "append-only `Human — Marco:` entry reflects something Marco actually stated; "
                    "it does not authorize another governed field."
                ),
            },
            "resume_status": {
                "type": "string",
                "enum": ["pending-research", "pending-verification"],
            },
        },
    },
    SUBMIT_COMMAND.name: {
        "required": ["submission_id"],
        "properties": {"submission_id": dict(DISH_UUID_SCHEMA)},
    },
    RENEW_LEASE_COMMAND.name: {
        "required": ["operation_id"],
        "properties": {"operation_id": dict(DISH_UUID_SCHEMA)},
    },
    QUALIFY_FILE_TRANSPORT_COMMAND.name: {
        "required": ["expected_sha256", "expected_bytes"],
        "properties": {
            "expected_sha256": {
                "type": "string",
                "description": (
                    "Lowercase hex SHA-256 the fetched file must match exactly."
                ),
            },
            "expected_bytes": {
                "type": "number",
                "description": (
                    "Exact byte count the fetched file must match exactly. Dish enforces "
                    f"a {QUALIFY_FILE_TRANSPORT_MAX_BYTES}-byte Gate A ceiling regardless "
                    "of this value."
                ),
            },
        },
    },
}

OPENAI_FILE_ID_REFS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 1,
    # OpenAI requires string items in the imported schema, then expands them into
    # file-reference objects before sending the HTTP request to the Action.
    "items": {"type": "string"},
    "description": (
        "Exactly one Code-Interpreter-produced file to fetch and qualify. OpenAI "
        "expands the selected file into id, name, mime_type, and download_link fields "
        "at runtime."
    ),
}

OPENAI_FILE_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "mime_type", "content"],
        "properties": {
            "name": {"type": "string"},
            "mime_type": {"type": "string"},
            "content": {
                "type": "string",
                "format": "byte",
                "description": "Base64-encoded receipt bytes returned inline to Code Interpreter.",
            },
        },
    },
}


def action_openapi_argument_schema(command: str) -> dict[str, Any]:
    """Return the public Action schema, including route-specific shapes."""
    if command == "read":
        base = ARGUMENT_SCHEMAS["read"]["properties"]

        def read_variant(identity_field: str) -> dict[str, Any]:
            return {
                "type": "object",
                "additionalProperties": False,
                "required": [identity_field, "agent"],
                "properties": {
                    identity_field: deepcopy(base[identity_field]),
                    "agent": deepcopy(base["agent"]),
                },
            }

        return {"oneOf": [read_variant("dish_id"), read_variant("task_gid")]}
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
                    "prepared_operation_id",
                    required=("change_level", "change_reason"),
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
                "enum": [correction],
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
                approve_variant(correction, with_file_text=(correction == "small"))
                for correction in APPROVAL_CORRECTIONS
            ],
            "discriminator": {"propertyName": "correction"},
        }
    if command != "reject":
        return action_argument_schema(command)

    base = ARGUMENT_SCHEMAS["reject"]["properties"]
    common = {name: deepcopy(base[name]) for name in ("submission_id", "agent", "reason")}
    blocker_fields = ("blocker_metric", "blocker_actual", "blocker_limit", "blocker_delta", "blocker_unit", "blocker_basis")

    def variant(route: str, *, extra: tuple[str, ...], required: tuple[str, ...]) -> dict[str, Any]:
        properties = deepcopy(common)
        properties["route"] = {
            "type": "string",
            "enum": [route],
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

    variants = {
        "large": dict(
            extra=("model", "file_text", "governed_change_fields"),
            required=("model", "file_text"),
        ),
        "evidence": dict(
            extra=("resume_status", *blocker_fields),
            required=("resume_status",),
        ),
        "human-review": dict(
            extra=(
                "resume_status", *blocker_fields, "human_review_confirmed",
                "human_review_basis", "repairs_considered", "human_review_options",
            ),
            required=("resume_status", "human_review_options"),
        ),
    }
    return {
        "oneOf": [variant(route, **variants[route]) for route in REJECTION_ROUTES],
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
    elif expected == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "object":
        valid = isinstance(value, dict)
    if not valid:
        raise _argument_error(
            f"{field} has the wrong type", "argument_type_invalid", field=field
        )
    if expected == "array":
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise _argument_error(
                f"{field} has too few items", "argument_item_count_invalid", field=field
            )
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise _argument_error(
                f"{field} has too many items", "argument_item_count_invalid", field=field
            )
        item_schema = schema.get("items") or {}
        for index, item in enumerate(value):
            _validate_scalar(f"{field}[{index}]", item, item_schema)
    if expected == "object":
        properties = schema.get("properties") or {}
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            raise _argument_error(
                f"{field}.{missing[0]} is required",
                "argument_required",
                field=f"{field}.{missing[0]}",
            )
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise _argument_error(
                    f"{field}.{extras[0]} is not accepted",
                    "argument_unexpected",
                    field=f"{field}.{extras[0]}",
                )
        for name, nested in value.items():
            if name in properties:
                _validate_scalar(f"{field}.{name}", nested, properties[name])
    if "enum" in schema and value not in schema["enum"]:
        raise _argument_error(
            f"{field} has an unsupported value", "argument_value_invalid", field=field
        )
    if "const" in schema and value != schema["const"]:
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
    if command == QUALIFY_FILE_TRANSPORT_COMMAND.name:
        allowed_top = allowed_top | {"openaiFileIdRefs"}
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
        message = "client.request_id is required for mutations"
        if command == "inspect":
            message = (
                "client.request_id is required for inspect because inspect records durable "
                "Verification evidence. If the connected GPT Action schema does not expose "
                "client.request_id for inspect, refresh or re-import the Dish Action schema."
            )
        raise _argument_error(
            message,
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
    if command == "read":
        has_task = bool(str(arguments.get("task_gid") or "").strip())
        has_dish = bool(str(arguments.get("dish_id") or "").strip())
        if has_task == has_dish:
            raise _argument_error(
                "read requires exactly one of task_gid or dish_id",
                "read_identity_required",
                field="dish_id",
            )
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
                "Verification targets are only for an exact Dish-returned abandonment "
                "continuation; supply both returned values together, or omit both for an "
                "ordinary Verification start",
                "argument_required",
                field=missing,
            )
    arguments = dict(arguments)
    if command == QUALIFY_FILE_TRANSPORT_COMMAND.name:
        expected_sha256 = arguments.get("expected_sha256")
        if not isinstance(expected_sha256, str) or not _SHA256_HEX_RE.fullmatch(
            expected_sha256
        ):
            raise _argument_error(
                "expected_sha256 must be a lowercase 64-character hex SHA-256",
                "argument_value_invalid",
                field="expected_sha256",
            )
        expected_bytes = arguments.get("expected_bytes")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes <= 0
            or expected_bytes > QUALIFY_FILE_TRANSPORT_MAX_BYTES
        ):
            raise _argument_error(
                "expected_bytes must be a positive integer up to "
                f"{QUALIFY_FILE_TRANSPORT_MAX_BYTES}",
                "argument_value_invalid",
                field="expected_bytes",
            )
        file_refs = request.get("openaiFileIdRefs")
        if not isinstance(file_refs, list) or len(file_refs) != 1:
            raise _argument_error(
                "openaiFileIdRefs must contain exactly one file",
                "openai_file_refs_invalid",
                field="openaiFileIdRefs",
            )
        file_ref = file_refs[0]
        if not isinstance(file_ref, dict):
            raise _argument_error(
                "openaiFileIdRefs[0] must be an object",
                "openai_file_refs_invalid",
                field="openaiFileIdRefs",
            )
        missing_file_fields = [
            name for name in QUALIFY_FILE_TRANSPORT_FILE_FIELDS if name not in file_ref
        ]
        if missing_file_fields:
            raise _argument_error(
                f"openaiFileIdRefs[0].{missing_file_fields[0]} is required",
                "openai_file_refs_invalid",
                field=f"openaiFileIdRefs.{missing_file_fields[0]}",
            )
        extra_file_fields = sorted(set(file_ref) - set(QUALIFY_FILE_TRANSPORT_FILE_FIELDS))
        if extra_file_fields:
            raise _argument_error(
                f"openaiFileIdRefs[0].{extra_file_fields[0]} is not accepted",
                "openai_file_refs_invalid",
                field=f"openaiFileIdRefs.{extra_file_fields[0]}",
            )
        for name in QUALIFY_FILE_TRANSPORT_FILE_FIELDS:
            value = file_ref[name]
            if not isinstance(value, str) or not value.strip():
                raise _argument_error(
                    f"openaiFileIdRefs[0].{name} is required",
                    "openai_file_refs_invalid",
                    field=f"openaiFileIdRefs.{name}",
                )
        arguments["file"] = dict(file_ref)
    return dict(client), arguments
