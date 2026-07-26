"""Structurally read-only access to unsupported legacy workflow records."""
from __future__ import annotations

import sqlite3

from .constants import SUPPORTED_PROTOCOL_VERSION
from .errors import DishRuleError

_MUTATING_COMMANDS = frozenset({"prepare", "approve", "reject", "submit", "discard", "unblock", "recover"})


class LegacyReadOnlyAdapter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, submission_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()

    def assert_command_allowed(self, command: str, *, protocol_version: str) -> None:
        if command in _MUTATING_COMMANDS and protocol_version == SUPPORTED_PROTOCOL_VERSION:
            raise DishRuleError(
                "WRONG_STATE",
                "legacy submissions are read-only under the current protocol",
                rule="legacy_record_read_only",
            )
