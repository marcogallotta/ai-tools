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


def derive_operation_state(conn: sqlite3.Connection, backend, operation_id: str, *, schema=None) -> dict[str, object]:
    """Derive one authoritative current-operation view from persistence and live task.

    Legal actions are emitted only when the persisted phase, canonical task state,
    and current Cooking placement agree.
    """
    from .constants import COOKING_PROJECT_GID
    from .database import legal_operation_actions
    from .models import SectionRegistry
    from .task_document import parse_task_document, validate_task_document
    from .task_store import read_complete_task

    op = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    actions = legal_operation_actions(op)
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    live_status = None
    validation_rules: list[str] = []
    try:
        document = parse_task_document(f"{live.title}\n{live.notes}")
        live_status = document.state.values["Status"]
        validation_rules = [f.rule for f in validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema).findings]
    except Exception:
        document = None
        validation_rules = ["canonical_task_required"]

    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    phase = op["phase"]
    if validation_rules:
        actions = []
    elif phase == "await_verification" and (live_status != "pending-verification" or live.section_gid != registry.verification_queue_gid):
        actions = []
    elif phase == "await_verification":
        cycle = conn.execute("SELECT reviewed_identity FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL ORDER BY cycle_number DESC LIMIT 1", (operation_id,)).fetchone()
        actions = ["approve", "reject"] if cycle is not None and cycle["reviewed_identity"] else ["verify"]
    elif phase == "await_submission" and live_status != "ready":
        actions = []
    elif phase == "held_evidence" and live_status != "pending-evidence":
        actions = []
    elif phase == "held_human" and live_status != "pending-human-review":
        actions = []
    elif phase == "prepare_required" and op["status"] != "open":
        actions = []
    return {
        "status": op["status"],
        "phase": phase,
        "legal_actions": list(actions),
        "live_status": live_status,
        "live_section_gid": live.section_gid,
        "validation_rules": validation_rules,
    }
