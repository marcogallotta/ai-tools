"""Machine-classifiable errors for the guarded dish workflow."""

from typing import Any, Mapping

from .constants import DEFAULT_RETRYABLE_BY_CODE, EXIT_STATUS_BY_CODE


class DishRuleError(Exception):
    """Expected, machine-classifiable workflow error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        rule: str | None = None,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in EXIT_STATUS_BY_CODE:
            raise ValueError(f"unknown result code: {code}")
        super().__init__(message)
        self.code = code
        self.rule = rule
        self.retryable = (
            DEFAULT_RETRYABLE_BY_CODE[code] if retryable is None else retryable
        )
        self.details = dict(details or {})


class ReleaseResolutionError(DishRuleError):
    def __init__(self, rule: str, message: str, **details: Any) -> None:
        super().__init__(
            "VALIDATION_FAILED",
            message,
            rule=rule,
            retryable=False,
            details=details,
        )


class BackendFailure(DishRuleError):
    """Mapped Asana/transport failure with application certainty attached."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        phase: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.status = status
        self.phase = phase
