"""Canonical result decoding and validation for Dish service clients."""
from __future__ import annotations

from typing import Any

from dish_tool.constants import EXIT_STATUS_BY_CODE
from dish_tool.errors import DishRuleError
from dish_tool.results import RESULT_ENVELOPE_FIELD_SET


POSTGRES_RESULT_REQUIRED_FIELD_SET = frozenset(
    {"ok", "command", "code", "http_status", "retryable", "data"}
)
POSTGRES_RESULT_FIELD_SET = POSTGRES_RESULT_REQUIRED_FIELD_SET | {
    "request_replayed"
}

# The PostgreSQL command port's own CommandResult dataclass always carries both
# http_status and the full legacy envelope fields (dish_pg/command_port_common.py),
# so its real wire shape is the union of both known families, not either alone.
POSTGRES_LEGACY_HYBRID_FIELD_SET = RESULT_ENVELOPE_FIELD_SET | {"http_status"}
POSTGRES_LEGACY_HYBRID_REPLAY_FIELD_SET = POSTGRES_LEGACY_HYBRID_FIELD_SET | {
    "request_replayed"
}


def require_result_envelope(result: Any) -> dict[str, Any]:
    """Require the legacy result fields used by non-command client surfaces."""
    if not isinstance(result, dict):
        raise DishRuleError(
            "INTERNAL_ERROR",
            (
                "dish service returned a noncanonical command result; "
                "verify DISH_SERVICE_URL points to the correct listener"
            ),
            rule="service_response_invalid",
            details={"result_type": type(result).__name__},
        )
    present = set(result)
    missing = sorted(RESULT_ENVELOPE_FIELD_SET - present)
    if missing:
        raise DishRuleError(
            "INTERNAL_ERROR",
            (
                "dish service returned a noncanonical command result; "
                "verify DISH_SERVICE_URL points to the correct listener"
            ),
            rule="service_response_invalid",
            details={"missing_fields": missing},
        )
    return result


def validate_canonical_result(
    result: Any, *, expected_command: str
) -> dict[str, Any]:
    """Strictly validate a result before trusting a consequential response."""
    if not isinstance(result, dict):
        raise ValueError("command result is not a JSON object")
    present = set(result)
    missing = sorted(RESULT_ENVELOPE_FIELD_SET - present)
    extras = sorted(present - RESULT_ENVELOPE_FIELD_SET)
    if missing or extras:
        raise ValueError(
            f"command result fields are invalid: missing={missing}, extras={extras}"
        )
    if not isinstance(result["ok"], bool):
        raise ValueError("ok must be boolean")
    if result["command"] != expected_command:
        raise ValueError("command does not match the request")
    if result["code"] not in EXIT_STATUS_BY_CODE:
        raise ValueError("code is not recognized")
    if result["ok"] != (result["code"] == "OK"):
        raise ValueError("ok and code are inconsistent")
    for field in ("task_gid", "submission_id", "state"):
        if result[field] is not None and not isinstance(result[field], str):
            raise ValueError(f"{field} must be a string or null")
    if not isinstance(result["retryable"], bool):
        raise ValueError("retryable must be boolean")
    if not isinstance(result["allowed_actions"], list) or not all(
        isinstance(item, str) for item in result["allowed_actions"]
    ):
        raise ValueError("allowed_actions must be a string array")
    if not isinstance(result["data"], dict):
        raise ValueError("data must be an object")
    if not isinstance(result["errors"], list) or not all(
        isinstance(item, dict) for item in result["errors"]
    ):
        raise ValueError("errors must be an object array")
    return result


def validate_hybrid_result(
    result: Any, *, expected_command: str
) -> dict[str, Any]:
    """Validate the PostgreSQL command port's legacy-envelope-plus-http_status shape.

    Unlike ``validate_canonical_result``, ``code`` is not restricted to the legacy
    ``EXIT_STATUS_BY_CODE`` set: PostgreSQL command handlers raise their own
    command-specific rule codes (e.g. ``AUTHORITY_CONTENTION``) that never existed
    in the legacy enum.
    """
    if not isinstance(result["ok"], bool):
        raise ValueError("ok must be boolean")
    if result["command"] != expected_command:
        raise ValueError("command does not match the request")
    if not isinstance(result["code"], str):
        raise ValueError("code must be a string")
    if result["ok"] != (result["code"] == "OK"):
        raise ValueError("ok and code are inconsistent")
    for field in ("task_gid", "submission_id", "state"):
        if result[field] is not None and not isinstance(result[field], str):
            raise ValueError(f"{field} must be a string or null")
    if not isinstance(result["retryable"], bool):
        raise ValueError("retryable must be boolean")
    if not isinstance(result["allowed_actions"], list) or not all(
        isinstance(item, str) for item in result["allowed_actions"]
    ):
        raise ValueError("allowed_actions must be a string array")
    if not isinstance(result["data"], dict):
        raise ValueError("data must be an object")
    if not isinstance(result["errors"], list) or not all(
        isinstance(item, dict) for item in result["errors"]
    ):
        raise ValueError("errors must be an object array")
    http_status = result["http_status"]
    if (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 100 <= http_status <= 599
    ):
        raise ValueError("http_status must be an integer from 100 through 599")
    if "request_replayed" in result and not isinstance(
        result["request_replayed"], bool
    ):
        raise ValueError("request_replayed must be boolean")
    return result


def validate_command_result(
    result: Any, *, expected_command: str
) -> dict[str, Any]:
    """Validate exactly one request-bound legacy or PostgreSQL result family."""
    if not isinstance(result, dict):
        raise ValueError("command result is not a JSON object")

    present = set(result)
    if present == RESULT_ENVELOPE_FIELD_SET:
        return validate_canonical_result(result, expected_command=expected_command)
    if present in (
        POSTGRES_LEGACY_HYBRID_FIELD_SET,
        POSTGRES_LEGACY_HYBRID_REPLAY_FIELD_SET,
    ):
        return validate_hybrid_result(result, expected_command=expected_command)
    if present not in (
        POSTGRES_RESULT_REQUIRED_FIELD_SET,
        POSTGRES_RESULT_FIELD_SET,
    ):
        raise ValueError("command result fields do not match a supported family")

    if not isinstance(result["ok"], bool):
        raise ValueError("ok must be boolean")
    if result["command"] != expected_command:
        raise ValueError("command does not match the request")
    if not isinstance(result["code"], str):
        raise ValueError("code must be a string")
    if result["ok"] != (result["code"] == "OK"):
        raise ValueError("ok and code are inconsistent")
    if not isinstance(result["retryable"], bool):
        raise ValueError("retryable must be boolean")
    if not isinstance(result["data"], dict):
        raise ValueError("data must be an object")
    http_status = result["http_status"]
    if (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 100 <= http_status <= 599
    ):
        raise ValueError("http_status must be an integer from 100 through 599")
    if "request_replayed" in result and not isinstance(
        result["request_replayed"], bool
    ):
        raise ValueError("request_replayed must be boolean")
    return result
