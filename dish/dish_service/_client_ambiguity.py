"""Fail-closed handling for consequential Dish client responses."""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Mapping

from dish_tool.errors import DishRuleError
from dish_tool.results import result_envelope

from ._client_results import validate_canonical_result, validate_command_result
from ._client_transport import AmbiguousResponseError
from .command_spec import REPLAY_SAFE_COMMANDS


_AMBIGUOUS_RESPONSE_REPLAY_COMMANDS = frozenset(
    {"inspect", "apply-proposal", "safe-reclaim"}
)

ResultRequest = Callable[..., Any]
JSONRequest = Callable[..., Any]


def request_id_for_command(command: str, request_id: str | None) -> str | None:
    if request_id is None and command in REPLAY_SAFE_COMMANDS:
        return str(uuid.uuid4())
    return request_id


def ambiguous_command_result(
    *, command: str, request_id: str, run_id: str
) -> dict[str, Any]:
    return result_envelope(
        command=command,
        ok=False,
        code="BACKEND_UNCERTAIN",
        retryable=False,
        allowed_actions=[],
        data={
            "message": "the request may have reached the service, but no authoritative response was received",
            "request_id": request_id,
            "run_id": run_id,
            "request_replay_required": True,
            "required_next_action": "retry_exact_request",
            "safe_to_retry": False,
        },
        errors=[{"rule": "service_response_ambiguous"}],
    )


def command_result_request(
    *,
    command: str,
    path: str,
    request_id: str | None,
    payload: Mapping[str, Any],
    run_id: str,
    result_request: ResultRequest,
) -> dict[str, Any]:
    ambiguous_response_requires_replay = (
        command in _AMBIGUOUS_RESPONSE_REPLAY_COMMANDS and request_id is not None
    )
    try:
        result = result_request(
            path,
            method="POST",
            payload=payload,
            ambiguous_after_dispatch=ambiguous_response_requires_replay,
        )
    except AmbiguousResponseError:
        return ambiguous_command_result(
            command=command, request_id=request_id, run_id=run_id
        )
    except DishRuleError as exc:
        if (
            ambiguous_response_requires_replay
            and exc.rule == "service_response_invalid"
        ):
            return ambiguous_command_result(
                command=command, request_id=request_id, run_id=run_id
            )
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        if ambiguous_response_requires_replay:
            return ambiguous_command_result(
                command=command, request_id=request_id, run_id=run_id
            )
        raise
    try:
        return validate_command_result(result, expected_command=command)
    except (ValueError, TypeError) as exc:
        if ambiguous_response_requires_replay:
            return ambiguous_command_result(
                command=command, request_id=request_id, run_id=run_id
            )
        raise DishRuleError(
            "INTERNAL_ERROR",
            (
                "dish service returned a noncanonical command result; "
                "verify DISH_SERVICE_URL points to the correct listener"
            ),
            rule="service_response_invalid",
            details={"validation_error": str(exc)},
        ) from exc


def ambiguous_expire_lease_result(
    *, request_id: str, task_gid: str | None, run_id: str
) -> dict[str, Any]:
    return result_envelope(
        command="expire-lease",
        ok=False,
        code="BACKEND_UNCERTAIN",
        task_gid=task_gid,
        retryable=False,
        allowed_actions=[],
        data={
            "message": "the service may have processed the lease-expiry request",
            "request_id": request_id,
            "run_id": run_id,
            "request_replay_required": True,
            "required_next_action": "retry_exact_request",
        },
        errors=[{"rule": "service_response_ambiguous"}],
    )


def expire_lease_result_request(
    *,
    payload: Mapping[str, Any],
    request_id: str,
    task_gid: str | None,
    run_id: str,
    request_json: JSONRequest,
) -> dict[str, Any]:
    try:
        decoded = request_json(
            "/v1/admin/leases/expire",
            method="POST",
            payload=payload,
            ambiguous_after_dispatch=True,
        )
        return validate_canonical_result(decoded, expected_command="expire-lease")
    except AmbiguousResponseError:
        return ambiguous_expire_lease_result(
            request_id=request_id, task_gid=task_gid, run_id=run_id
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return ambiguous_expire_lease_result(
            request_id=request_id, task_gid=task_gid, run_id=run_id
        )
