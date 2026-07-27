"""Strict validation for identifiers supplied across the HTTP trust boundary."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from dish_tool.errors import DishRuleError

_ASANA_GID_FIELDS = {"task_gid", "project_gid", "section_gid"}
_DISH_UUID_FIELDS = {
    "submission_id",
    "operation_id",
    "cycle_id",
    "verification_cycle_id",
}
_NUMERIC_GID_RE = re.compile(r"[1-9][0-9]*")


def _invalid_identifier(field: str, rule: str, message: str) -> DishRuleError:
    return DishRuleError(
        "INVALID_ARGUMENT",
        message,
        rule=rule,
        retryable=False,
        details={"field": field},
    )


def require_asana_gid(value: Any, *, field: str) -> str:
    """Return an exact decimal Asana GID or reject it without backend access."""
    if not isinstance(value, str) or _NUMERIC_GID_RE.fullmatch(value) is None:
        raise _invalid_identifier(
            field,
            "numeric_identifier_required",
            f"{field} must be a numeric identifier",
        )
    return value


def require_dish_uuid(value: Any, *, field: str) -> str:
    """Return a canonical Dish UUID or reject it without database access."""
    if not isinstance(value, str):
        raise _invalid_identifier(
            field,
            "uuid_identifier_required",
            f"{field} must be a UUID identifier",
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise _invalid_identifier(
            field,
            "uuid_identifier_required",
            f"{field} must be a UUID identifier",
        ) from None
    if str(parsed) != value:
        raise _invalid_identifier(
            field,
            "uuid_identifier_required",
            f"{field} must be a canonical UUID identifier",
        )
    return value


def validate_identifier_fields(
    values: Mapping[str, Any], *, allow_null: bool = False
) -> None:
    """Validate every recognized identifier present in one request mapping."""
    for field in _ASANA_GID_FIELDS:
        if field in values:
            if allow_null and values[field] is None:
                continue
            require_asana_gid(values[field], field=field)
    for field in _DISH_UUID_FIELDS:
        if field in values:
            if allow_null and values[field] is None:
                continue
            require_dish_uuid(values[field], field=field)
