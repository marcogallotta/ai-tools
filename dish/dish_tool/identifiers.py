"""Strict reusable validation for Dish and Asana identifiers."""

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
    "lease_id",
    "cycle_id",
    "verification_cycle_id",
    "intent_challenge_id",
}
NIL_DISH_UUID = "00000000-0000-0000-0000-000000000000"
CANONICAL_DISH_UUID_PATTERN = (
    rf"^(?!{NIL_DISH_UUID}$)"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
CANONICAL_DISH_UUID_LENGTH = 36
CANONICAL_DISH_UUID_SCHEMA = {
    "type": "string",
    "format": "uuid",
    "pattern": CANONICAL_DISH_UUID_PATTERN,
    "minLength": CANONICAL_DISH_UUID_LENGTH,
    "maxLength": CANONICAL_DISH_UUID_LENGTH,
}
_NUMERIC_GID_RE = re.compile(r"[1-9][0-9]*")
_CANONICAL_DISH_UUID_RE = re.compile(CANONICAL_DISH_UUID_PATTERN)
MAX_ASANA_GID = (1 << 63) - 1
MAX_ASANA_GID_LENGTH = len(str(MAX_ASANA_GID))

ASANA_IDENTITY_SCHEME = "dish-asana-gid-uuid5-v1"
# uuid5(uuid.NAMESPACE_URL, "urn:dish:identity:asana:v1")
ASANA_IDENTITY_NAMESPACE = uuid.UUID("a8ad7ec4-ec82-5764-b89e-1fed9c62e4a1")
_ASANA_IDENTITY_KINDS = frozenset({"task", "project", "section"})


def stable_dish_uuid_for_asana_identity(entity_kind: str, asana_gid: Any) -> uuid.UUID:
    """Return the stable PostgreSQL Dish UUID for one typed legacy Asana identity.

    This is the shared identity authority used by both migration/import tooling and
    legacy admin target resolution.  It intentionally validates only the canonical
    positive-decimal spelling here; surface-specific range checks remain with the
    caller.
    """
    if entity_kind not in _ASANA_IDENTITY_KINDS:
        raise ValueError(f"unsupported Asana identity kind: {entity_kind}")
    gid = str(asana_gid or "").strip()
    if _NUMERIC_GID_RE.fullmatch(gid) is None:
        raise ValueError(f"{entity_kind}_gid must be a canonical positive decimal Asana GID")
    return uuid.uuid5(ASANA_IDENTITY_NAMESPACE, f"{entity_kind}:{gid}")


def _invalid_identifier(
    field: str, rule: str, message: str, *, expected_format: str | None = None
) -> DishRuleError:
    details = {"field": field}
    if expected_format is not None:
        details["expected_format"] = expected_format
    return DishRuleError(
        "INVALID_ARGUMENT",
        message,
        rule=rule,
        retryable=False,
        details=details,
    )


def require_asana_gid(value: Any, *, field: str) -> str:
    """Return an exact decimal Asana GID or reject it without backend access."""
    if not isinstance(value, str) or _NUMERIC_GID_RE.fullmatch(value) is None:
        raise _invalid_identifier(
            field,
            "numeric_identifier_required",
            f"{field} must be a numeric identifier",
        )
    if len(value) > MAX_ASANA_GID_LENGTH or int(value) > MAX_ASANA_GID:
        raise _invalid_identifier(
            field,
            "numeric_identifier_out_of_range",
            f"{field} exceeds the supported Asana identifier range",
            expected_format=f"decimal integer from 1 to {MAX_ASANA_GID}",
        )
    return value


def require_dish_uuid(value: Any, *, field: str) -> str:
    """Return a non-nil canonical Dish UUID or reject it without database access."""
    if not isinstance(value, str) or _CANONICAL_DISH_UUID_RE.fullmatch(value) is None:
        raise _invalid_identifier(
            field,
            "uuid_identifier_required",
            f"{field} must be a non-nil canonical lowercase UUID in 8-4-4-4-12 form",
            expected_format="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise _invalid_identifier(
            field,
            "uuid_identifier_required",
            f"{field} must be a non-nil canonical lowercase UUID in 8-4-4-4-12 form",
            expected_format="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        ) from None
    if parsed.int == 0 or str(parsed) != value:
        raise _invalid_identifier(
            field,
            "uuid_identifier_required",
            f"{field} must be a non-nil canonical lowercase UUID in 8-4-4-4-12 form",
            expected_format="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        )
    return value


def validate_identifier_fields(
    values: Mapping[str, Any],
    *,
    allow_null: bool = False,
    skip_fields: frozenset[str] = frozenset(),
) -> None:
    """Validate every recognized identifier present in one request mapping.

    `skip_fields` excludes fields whose format is ambiguous at this generic
    boundary check (e.g. an admin target that may be an exact dish UUID or an
    Asana task GID/URL) and is validated later, closer to its actual meaning.
    """
    for field in _ASANA_GID_FIELDS:
        if field in values and field not in skip_fields:
            if allow_null and values[field] is None:
                continue
            require_asana_gid(values[field], field=field)
    for field in _DISH_UUID_FIELDS:
        if field in values and field not in skip_fields:
            if allow_null and values[field] is None:
                continue
            require_dish_uuid(values[field], field=field)
