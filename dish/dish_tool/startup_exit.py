"""Stable process exit statuses for service-manager startup classification."""
from __future__ import annotations

from dish_tool.errors import DishRuleError

# EX_CONFIG: deterministic operator/configuration action is required.  systemd
# units use RestartPreventExitStatus=78 so these PostgreSQL failures remain
# failed instead of entering an automatic restart loop.
NON_RETRYABLE_STARTUP_EXIT_STATUS = 78
RETRYABLE_STARTUP_EXIT_STATUS = 1


def startup_exit_status(error: DishRuleError) -> int:
    """Map deterministic PostgreSQL startup failures away from retryable exit 1."""
    if (
        error.rule
        and error.rule.startswith("postgresql_")
        and error.retryable is False
    ):
        return NON_RETRYABLE_STARTUP_EXIT_STATUS
    return RETRYABLE_STARTUP_EXIT_STATUS
