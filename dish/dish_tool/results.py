"""Common result envelopes and exit-status mapping."""

from typing import Any, Mapping, Sequence

from .constants import (
    ALLOWED_ACTIONS_BY_STATE,
    DEFAULT_RETRYABLE_BY_CODE,
    EXIT_STATUS_BY_CODE,
)
from .errors import DishRuleError


def allowed_actions_for_state(state: str | None) -> list[str]:
    if state not in ALLOWED_ACTIONS_BY_STATE:
        return []
    return list(ALLOWED_ACTIONS_BY_STATE[state])


def result_envelope(
    *,
    command: str,
    ok: bool = True,
    code: str = "OK",
    task_gid: str | None = None,
    submission_id: str | None = None,
    state: str | None = None,
    retryable: bool | None = None,
    allowed_actions: Sequence[str] | None = None,
    data: Mapping[str, Any] | None = None,
    errors: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if code not in EXIT_STATUS_BY_CODE:
        raise ValueError(f"unknown result code: {code}")
    if ok and code != "OK":
        raise ValueError("successful result must use code OK")
    if not ok and code == "OK":
        raise ValueError("failed result must use a failure code")
    if retryable is None:
        retryable = DEFAULT_RETRYABLE_BY_CODE[code]
    if allowed_actions is None:
        allowed_actions = allowed_actions_for_state(state)
    return {
        "ok": bool(ok),
        "command": command,
        "code": code,
        "task_gid": task_gid,
        "submission_id": submission_id,
        "state": state,
        "retryable": bool(retryable),
        "allowed_actions": list(allowed_actions),
        "data": dict(data or {}),
        "errors": [dict(error) for error in (errors or [])],
    }


def error_envelope(
    command: str,
    error: DishRuleError,
    *,
    task_gid: str | None = None,
    submission_id: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    rule_error = [dict(item) for item in error.errors]
    if error.rule:
        item = {"rule": error.rule}
        item.update(error.details)
        rule_error.append(item)
    return result_envelope(
        command=command,
        ok=False,
        code=error.code,
        task_gid=task_gid,
        submission_id=submission_id,
        state=state,
        retryable=error.retryable,
        errors=rule_error,
        data={"message": str(error)},
    )



def exit_status(code: str) -> int:
    return EXIT_STATUS_BY_CODE.get(code, 1)
