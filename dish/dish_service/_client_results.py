"""Canonical result decoding and validation for Dish service clients."""
from __future__ import annotations

from typing import Any

from dish_tool.constants import EXIT_STATUS_BY_CODE
from dish_tool.errors import DishRuleError
from dish_tool.results import RESULT_ENVELOPE_FIELD_SET


def require_result_envelope(result: Any) -> dict[str, Any]:
    """Require the shared result envelope fields used by every client surface."""
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
