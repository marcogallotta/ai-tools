"""Explicit routing boundary between current operations and legacy records."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .constants import SUPPORTED_PROTOCOL_VERSION
from .errors import DishRuleError

_MUTATING_COMMANDS = frozenset({"prepare", "approve", "reject", "submit", "discard", "unblock"})


@dataclass(frozen=True)
class RoutedTarget:
    generation: str
    row: sqlite3.Row | None


class LegacyReadOnlyAdapter:
    """Legacy records are inspectable; current-protocol mutation is forbidden."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, submission_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()

    def assert_command_allowed(self, command: str, *, protocol_version: str) -> None:
        # Older protocol fixtures remain executable solely for explicit upgrade
        # compatibility. Current-protocol runtime never writes a legacy record.
        if command in _MUTATING_COMMANDS and protocol_version == SUPPORTED_PROTOCOL_VERSION:
            raise DishRuleError(
                "WRONG_STATE",
                "legacy submissions are read-only under the current protocol",
                rule="legacy_record_read_only",
            )


class OperationApplicationService:
    """Single authority for choosing the current or legacy application path."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.legacy = LegacyReadOnlyAdapter(conn)

    def route(self, identifier: str, *, command: str, protocol_version: str) -> RoutedTarget:
        operation = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (identifier,)
        ).fetchone()
        if operation is not None:
            return RoutedTarget("operation", operation)
        legacy = self.legacy.get(identifier)
        if legacy is not None:
            self.legacy.assert_command_allowed(command, protocol_version=protocol_version)
            return RoutedTarget("legacy", legacy)
        return RoutedTarget("missing", None)
