"""Transport-neutral boundary for current dish operations and legacy records."""
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
        if command in _MUTATING_COMMANDS and protocol_version == SUPPORTED_PROTOCOL_VERSION:
            raise DishRuleError(
                "WRONG_STATE",
                "legacy submissions are read-only under the current protocol",
                rule="legacy_record_read_only",
            )


class CurrentWorkflowService:
    """Authoritative live-state and legal-action service for current operations.

    CLI and future HTTP transports call this boundary instead of independently
    interpreting operation phase, task status, placement, cycle, and signoff.
    """

    def __init__(self, conn: sqlite3.Connection, backend) -> None:
        self.conn = conn
        self.backend = backend

    def operation(self, operation_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
        return row

    def authoritative_view(self, operation_id: str, *, schema=None) -> dict[str, object]:
        from .constants import COOKING_PROJECT_GID
        from .database import legal_operation_actions
        from .models import SectionRegistry
        from .task_document import parse_task_document, validate_task_document
        from .task_store import read_complete_task

        op = self.operation(operation_id)
        actions = legal_operation_actions(op)
        live = read_complete_task(
            self.backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
        )
        live_status = None
        validation_rules: list[str] = []
        try:
            document = parse_task_document(f"{live.title}\n{live.notes}")
            live_status = document.state.values["Status"]
            validation_rules = [
                finding.rule
                for finding in validate_task_document(
                    document,
                    expected_schema_version=op["schema_version"],
                    schema=schema,
                ).findings
            ]
        except Exception:
            validation_rules = ["canonical_task_required"]

        registry = SectionRegistry.from_sections(
            self.backend.list_sections(COOKING_PROJECT_GID)
        )
        phase = op["phase"]
        if validation_rules:
            actions = []
        elif phase == "await_verification" and (
            live_status != "pending-verification"
            or live.section_gid != registry.verification_queue_gid
        ):
            actions = []
        elif phase == "await_verification":
            cycle = self.conn.execute(
                """SELECT reviewed_identity FROM verification_cycles
                   WHERE operation_id=? AND completed_at IS NULL
                   ORDER BY cycle_number DESC LIMIT 1""",
                (operation_id,),
            ).fetchone()
            actions = (
                ["approve", "reject"]
                if cycle is not None and cycle["reviewed_identity"]
                else ["verify"]
            )
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

    def assert_action(self, operation_id: str, action: str, *, schema=None) -> dict[str, object]:
        view = self.authoritative_view(operation_id, schema=schema)
        if action not in view["legal_actions"]:
            raise DishRuleError(
                "WRONG_STATE",
                f"{action} is not legal for the current operation state",
                rule="operation_action_not_allowed",
                details={"action": action, "authoritative_view": view},
            )
        return view


class OperationApplicationService:
    """Single generation router plus current-workflow service boundary."""

    def __init__(self, conn: sqlite3.Connection, backend=None) -> None:
        self.conn = conn
        self.legacy = LegacyReadOnlyAdapter(conn)
        self.current = None if backend is None else CurrentWorkflowService(conn, backend)

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

    def authoritative_view(self, operation_id: str, *, schema=None) -> dict[str, object]:
        if self.current is None:
            raise RuntimeError("current workflow service requires a backend")
        return self.current.authoritative_view(operation_id, schema=schema)


def derive_operation_state(conn: sqlite3.Connection, backend, operation_id: str, *, schema=None) -> dict[str, object]:
    """Compatibility wrapper for callers not yet constructed with the service."""
    return CurrentWorkflowService(conn, backend).authoritative_view(operation_id, schema=schema)
