"""Transport-neutral authority for current dish workflow operations."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, TypeVar

from .errors import DishRuleError
from .legacy_adapter import LegacyReadOnlyAdapter
from .task_gateway import ExactTaskGateway
from .workflow_policy import WorkflowSnapshot, legal_actions
from .workflow_repository import WorkflowRepository

T = TypeVar("T")


@dataclass(frozen=True)
class RoutedTarget:
    generation: str
    row: sqlite3.Row | None


class CurrentWorkflowService:
    """Single authorization and result-state boundary for current mutations."""

    def __init__(self, conn: sqlite3.Connection, backend) -> None:
        self.conn = conn
        self.backend = backend
        self.repository = WorkflowRepository(conn)
        self.gateway = ExactTaskGateway(conn, backend)

    def operation(self, operation_id: str) -> sqlite3.Row:
        row = self.repository.operation(operation_id)
        if row is None:
            raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
        return row

    def _snapshot(self, operation_id: str, *, schema=None) -> tuple[WorkflowSnapshot, dict[str, object]]:
        from .constants import COOKING_PROJECT_GID
        from .models import SectionRegistry
        from .task_document import parse_task_document, validate_task_document

        op = self.operation(operation_id)
        live = self.gateway.read(task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
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

        registry = SectionRegistry.from_sections(self.backend.list_sections(COOKING_PROJECT_GID))
        cycle = self.conn.execute(
            """SELECT * FROM verification_cycles
               WHERE operation_id=?
               ORDER BY cycle_number DESC LIMIT 1""",
            (operation_id,),
        ).fetchone()
        cycle_reviewed = False
        if cycle is not None and cycle["completed_at"] is None:
            proof_ok = bool(str(cycle["run_id"] or "").strip() or str(cycle["independence_attestation"] or "").strip())
            binding_ok = bool(cycle["reviewed_content_version_id"] and cycle["reviewed_identity"] and cycle["verifier_agent"] and proof_ok)
            actor = None
            if binding_ok:
                actor = self.conn.execute(
                    """SELECT 1 FROM operation_actor_facts
                         WHERE task_gid=? AND operation_id=? AND role='verifier'
                           AND agent=? AND candidate_identity=?
                           AND COALESCE(run_id,'')=COALESCE(?, '')
                           AND COALESCE(independence_attestation,'')=COALESCE(?, '')
                         LIMIT 1""",
                    (op["task_gid"], operation_id, cycle["verifier_agent"], cycle["reviewed_identity"], cycle["run_id"], cycle["independence_attestation"]),
                ).fetchone()
            cycle_reviewed = bool(binding_ok and actor is not None)
        snapshot = WorkflowSnapshot(
            operation_status=op["status"],
            operation_phase=op["phase"],
            persisted_actions=tuple(self.repository.legal_actions(op)),
            live_status=live_status,
            live_section_gid=live.section_gid,
            verification_queue_gid=registry.verification_queue_gid,
            cycle_reviewed=cycle_reviewed,
            validation_rules=tuple(validation_rules),
        )
        facts = {
            "status": op["status"],
            "phase": op["phase"],
            "live_status": live_status,
            "live_section_gid": live.section_gid,
            "validation_rules": validation_rules,
        }
        return snapshot, facts

    def authoritative_view(self, operation_id: str, *, schema=None) -> dict[str, object]:
        snapshot, facts = self._snapshot(operation_id, schema=schema)
        facts["legal_actions"] = legal_actions(snapshot)
        return facts

    def assert_action(self, operation_id: str, action: str, *, schema=None) -> dict[str, object]:
        view = self.authoritative_view(operation_id, schema=schema)
        if action not in view["legal_actions"]:
            rule = "operation_action_not_allowed"
            message = f"{action} is not legal for the current operation state"
            if action == "verify" and view["phase"] == "await_verification":
                rule = "verification_placement_required"
                message = "task must currently be in Verification Queue"
            raise DishRuleError(
                "WRONG_STATE", message, rule=rule,
                details={"action": action, "authoritative_view": view},
            )
        return view

    def mutate(
        self,
        operation_id: str,
        action: str,
        executor: Callable[[], T],
        *,
        schema=None,
    ) -> tuple[T, dict[str, object]]:
        """Authorize, execute one use case, and return a fresh post-operation view."""
        self.assert_action(operation_id, action, schema=schema)
        result = executor()
        return result, self.authoritative_view(operation_id, schema=schema)

    def prepare(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "prepare", executor, schema=schema)

    def start_verification(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "verify", executor, schema=schema)

    def approve(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "approve", executor, schema=schema)

    def reject(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "reject", executor, schema=schema)

    def submit(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "submit", executor, schema=schema)

    def create_task(self, executor: Callable[[], T]) -> T:
        return executor()

    def start_operation(self, executor: Callable[[], T]) -> T:
        return executor()

    def resolve_hold(self, operation_id: str, action: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, action, executor, schema=schema)

    def reopen_two_pass(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        view = self.authoritative_view(operation_id, schema=schema)
        cycle = self.conn.execute(
            "SELECT outcome, route FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if view["phase"] != "held_human" or cycle is None or cycle["outcome"] != "two-pass-hold":
            raise DishRuleError(
                "WRONG_STATE", "reopen is legal only for a two-pass human hold",
                rule="two_pass_hold_required", details={"authoritative_view": view},
            )
        result = executor()
        return result, self.authoritative_view(operation_id, schema=schema)

    def recover(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        self.operation(operation_id)
        result = executor()
        return result, self.authoritative_view(operation_id, schema=schema)

    def cancel(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        op = self.operation(operation_id)
        if op["status"] not in {"open", "uncertain"}:
            raise DishRuleError("WRONG_STATE", "operation is not cancellable", rule="operation_not_cancellable")
        result = executor()
        return result, self.authoritative_view(operation_id, schema=schema)

    def authorize_governed_change(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        self.operation(operation_id)
        result = executor()
        return result, self.authoritative_view(operation_id, schema=schema)


class OperationApplicationService:
    """Generation router plus the current workflow mutation authority."""

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
