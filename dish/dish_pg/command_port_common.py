"""Shared value types and pure helpers for the PostgreSQL command port."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from dish_tool.dish_urls import dish_uuid_from_url
from dish_tool.errors import DishRuleError
from dish_tool.task_urls import task_gid_from_url


class CommandPortError(ValueError):
    """Base error for canonical command admission or execution."""


class CommandRuleError(CommandPortError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.data = dict(data or {})


class ArchiveNotRestingError(CommandRuleError):
    """Archive cannot safely terminalize unresolved workflow/effect authority."""

    def __init__(
        self,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("TASK_NOT_RESTING", message, data=data)


@dataclass(frozen=True)
class CommandCall:
    command_name: str
    arguments: Mapping[str, Any]
    owner_id: str
    principal_class: str
    run_id: uuid.UUID
    request_id: uuid.UUID | None
    now: datetime
    protocol_release: str = "protocol-1"


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: str
    code: str
    http_status: int
    data: Mapping[str, Any]
    retryable: bool = False
    request_replayed: bool = False
    task_gid: str | None = None
    submission_id: str | None = None
    state: str | None = None
    allowed_actions: tuple[str, ...] = ()
    errors: tuple[Mapping[str, Any], ...] = ()


SEMANTIC_PROPOSAL_PREFIX = "dish-pg-semantic-proposal-v1:"
SAFE_RECLAIM_REASON_PREFIX = "safe-reclaim:"


def json_safe(value: Any) -> Any:
    """Normalize typed canonical values to their durable JSON representation."""

    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def semantic_proposal_text(payload: Mapping[str, Any]) -> str:
    return SEMANTIC_PROPOSAL_PREFIX + json.dumps(
        json_safe(dict(payload)), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def decode_semantic_proposal_text(value: str) -> dict[str, Any] | None:
    if not value.startswith(SEMANTIC_PROPOSAL_PREFIX):
        return None
    try:
        decoded = json.loads(value[len(SEMANTIC_PROPOSAL_PREFIX) :])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CommandRuleError(
            "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
            "stored PostgreSQL semantic proposal metadata is not valid JSON",
        ) from exc
    if not isinstance(decoded, dict) or decoded.get("version") != 1:
        raise CommandRuleError(
            "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
            "stored PostgreSQL semantic proposal metadata has an unsupported version",
        )
    return decoded


def task_reference_from_dish(value: str) -> str | None:
    """Reduce a legacy Dish/Asana reference to a PostgreSQL-local task key."""

    clean = value.strip()
    if not clean:
        return None
    if clean.isdecimal():
        return clean
    if clean.startswith("/dishes/") or "://" in clean:
        try:
            return dish_uuid_from_url(clean)
        except DishRuleError:
            if clean.startswith("/dishes/"):
                return None
        try:
            return task_gid_from_url(clean)
        except DishRuleError:
            return None
    return clean