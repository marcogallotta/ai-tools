"""Observable, success-preserving processing of pending invocation-audit repairs."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, MutableMapping

from .database import process_command_audit_repairs

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditRepairAttempt:
    repaired: int = 0
    error_type: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_type is not None


def attempt_command_audit_repairs(
    conn: sqlite3.Connection, *, surface: str
) -> AuditRepairAttempt:
    """Process pending repairs without hiding a degraded repair worker.

    Command execution remains available because a pending invocation-audit repair
    is not authority for the current mutation. The failure is nevertheless logged
    and attached to the command result by ``attach_audit_repair_warning``.
    """

    try:
        return AuditRepairAttempt(repaired=process_command_audit_repairs(conn))
    except Exception as exc:  # explicit success-preserving observability boundary
        LOGGER.exception("pending invocation-audit repair processing failed", extra={"surface": surface})
        return AuditRepairAttempt(error_type=type(exc).__name__)


def attach_audit_repair_warning(
    result: MutableMapping[str, Any],
    attempt: AuditRepairAttempt,
    *,
    surface: str,
) -> None:
    if not attempt.failed:
        return
    data = dict(result.get("data") or {})
    data["audit_repair_processing_warning"] = {
        "kind": "pending_invocation_audit_repair",
        "surface": surface,
        "error_type": attempt.error_type,
        "current_command_committed": bool(result.get("ok")),
    }
    result["data"] = data
