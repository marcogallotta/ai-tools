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



def label_unsupported_legacy_workflow(
    result: Mapping[str, Any], *, diagnostic_read: bool = False
) -> dict[str, Any]:
    """Attach the explicit read-only compatibility contract to a result."""
    from .constants import (
        LEGACY_WORKFLOW_NAME,
        PROTOCOL_INCOMPATIBLE_MESSAGE,
        UNSUPPORTED_WORKFLOW_STATE,
    )

    labelled = dict(result)
    original_state = labelled.get("state")
    data = dict(labelled.get("data") or {})
    if original_state is not None and original_state != UNSUPPORTED_WORKFLOW_STATE:
        data.setdefault("legacy_state", original_state)
    data["compatibility"] = {
        "status": "unsupported",
        "workflow": LEGACY_WORKFLOW_NAME,
        "diagnostic_read_only": bool(diagnostic_read),
        "message": PROTOCOL_INCOMPATIBLE_MESSAGE,
    }
    labelled["data"] = data
    labelled["state"] = UNSUPPORTED_WORKFLOW_STATE
    labelled["retryable"] = False
    labelled["allowed_actions"] = []
    return labelled

def exit_status(code: str) -> int:
    return EXIT_STATUS_BY_CODE.get(code, 1)
