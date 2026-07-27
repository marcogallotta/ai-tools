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

        task_head = self.conn.execute(
            "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid=?",
            (op["task_gid"],),
        ).fetchone()
        required_identity = None if task_head is None else task_head["last_confirmed_identity"]
        required_section_gid = op["expected_section_gid"]
        required_cycle_exists = True
        signoff_bound = True
        held_baseline_matches = True
        phase = op["phase"]
        if phase == "await_verification":
            required_cycle_exists = bool(cycle is not None and cycle["completed_at"] is None)
            if cycle_reviewed:
                required_identity = cycle["reviewed_identity"]
            required_section_gid = registry.verification_queue_gid
        elif phase == "await_submission":
            approved = self.conn.execute(
                """SELECT * FROM verification_cycles
                     WHERE operation_id=? AND outcome='approved'
                     ORDER BY completed_at DESC LIMIT 1""",
                (operation_id,),
            ).fetchone()
            signoff_bound = bool(
                approved is not None
                and approved["signed_identity"]
                and approved["signed_content_version_id"]
                and op["signoff_completed_at"] is not None
            )
            required_identity = None if approved is None else approved["signed_identity"]
            # Submission deliberately preserves a manual placement or recognises
            # an already-applied destination move. Exact signed content remains mandatory.
            required_section_gid = None
        elif phase in {"held_evidence", "held_human"}:
            held = self.conn.execute(
                """SELECT * FROM verification_cycles
                     WHERE operation_id=? AND completed_at IS NOT NULL
                       AND (route IN ('evidence','human_review') OR outcome='two-pass-hold')
                     ORDER BY cycle_number DESC LIMIT 1""",
                (operation_id,),
            ).fetchone()
            required_cycle_exists = held is not None
            required_identity = None if held is None else held["hold_identity"]
            required_section_gid = None if held is None else held["hold_section_gid"]
            held_baseline_matches = bool(
                held is not None and held["hold_identity"] and held["hold_section_gid"]
                and live.identity == held["hold_identity"]
                and live.section_gid == held["hold_section_gid"]
            )
        destination_movement = None
        if op["movement_completed_at"] is not None and op["destination_movement_attempt_id"]:
            destination_movement = self.conn.execute(
                """SELECT confirmed_section_gid
                     FROM movement_attempts
                    WHERE attempt_id=? AND operation_id=?
                      AND purpose='destination_submission'
                      AND outcome='confirmed'
                      AND confirmed_section_gid=intended_section_gid""",
                (op["destination_movement_attempt_id"], operation_id),
            ).fetchone()
        if destination_movement is not None:
            required_section_gid = destination_movement["confirmed_section_gid"]
        required_section = (
            None if required_section_gid is None
            else registry.by_gid.get(required_section_gid)
        )
        required_section_name = None if required_section is None else required_section.name
        identity_matches = required_identity is None or live.identity == required_identity
        placement_matches = required_section_gid is None or live.section_gid == required_section_gid
        unresolved_rows = self.conn.execute(
            """SELECT 'write:' || attempt_id AS item FROM write_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 'movement:' || attempt_id AS item FROM movement_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               ORDER BY item""",
            (operation_id, operation_id),
        ).fetchall()
        unresolved_attempts = tuple(row["item"] for row in unresolved_rows)
        pending_steps = tuple(row["step_name"] for row in self.repository.pending_steps(operation_id))
        migration_required = bool(op["migration_reconciliation_required"])
        snapshot = WorkflowSnapshot(
            operation_status=op["status"],
            operation_phase=op["phase"],
            persisted_actions=tuple(self.repository.legal_actions(op)),
            live_status=live_status,
            live_section_gid=live.section_gid,
            verification_queue_gid=registry.verification_queue_gid,
            cycle_reviewed=cycle_reviewed,
            latest_cycle_outcome=None if cycle is None else cycle["outcome"],
            latest_cycle_route=None if cycle is None else cycle["route"],
            validation_rules=tuple(validation_rules),
            pending_steps=pending_steps,
            unresolved_attempts=unresolved_attempts,
            migration_reconciliation_required=migration_required,
            identity_matches=identity_matches,
            placement_matches=placement_matches,
            required_cycle_exists=required_cycle_exists,
            signoff_bound=signoff_bound,
            held_baseline_matches=held_baseline_matches,
        )
        recovery_reasons: list[str] = []
        if op["status"] == "uncertain":
            recovery_reasons.append("operation_uncertain")
        if pending_steps:
            recovery_reasons.append("pending_workflow_steps")
        if unresolved_attempts:
            recovery_reasons.append("unresolved_external_attempts")
        if migration_required:
            recovery_reasons.append(str(op["migration_reconciliation_reason"] or "migration_reconciliation_required"))
        facts = {
            "status": op["status"],
            "phase": op["phase"],
            "live_status": live_status,
            "live_identity": live.identity,
            "required_identity": required_identity,
            "identity_matches": identity_matches,
            "live_section_gid": live.section_gid,
            "required_section_gid": required_section_gid,
            "required_section_name": required_section_name,
            "placement_matches": placement_matches,
            "validation_rules": validation_rules,
            "pending_steps": list(pending_steps),
            "unresolved_attempts": list(unresolved_attempts),
            "required_cycle_exists": required_cycle_exists,
            "signoff_bound": signoff_bound,
            "held_baseline_matches": held_baseline_matches,
            "recovery_required": bool(recovery_reasons),
            "recovery_reasons": recovery_reasons,
        }
        return snapshot, facts

    def authoritative_view(self, operation_id: str, *, schema=None) -> dict[str, object]:
        snapshot, facts = self._snapshot(operation_id, schema=schema)
        facts["legal_actions"] = legal_actions(snapshot)
        return facts

    def assert_action(self, operation_id: str, action: str, *, schema=None) -> dict[str, object]:
        view = self.authoritative_view(operation_id, schema=schema)
        if action not in view["legal_actions"]:
            code = "WRONG_STATE"
            rule = "operation_action_not_allowed"
            message = f"{action} is not legal for the current operation state"
            # Terminal operations remain terminal even if their live task later
            # drifts. Report the lifecycle error before active-operation drift
            # diagnostics so retries receive a stable WRONG_STATE result.
            if view.get("status") not in {"open", "uncertain"}:
                pass
            elif view.get("recovery_required"):
                rule = "workflow_recovery_required"
                message = "operation requires recovery or migration reconciliation before any ordinary action"
            elif not view.get("identity_matches", True):
                code = "CONFLICT"
                if view.get("phase") == "await_submission":
                    rule = "post_signoff_content_drift"
                    message = "live content no longer matches the exact signed candidate"
                else:
                    rule = "live_task_drift"
                    message = "live task content does not match the authoritative workflow identity"
            elif not view.get("placement_matches", True):
                if view.get("phase") == "await_verification":
                    rule = "verification_placement_required"
                    message = "task must currently be in Verification Queue"
                else:
                    code = "CONFLICT"
                    rule = "live_task_placement_drift"
                    message = "live task placement does not match the authoritative workflow placement"
            elif not view.get("required_cycle_exists", True):
                rule = "verification_cycle_missing"
                message = "the required Verification cycle is missing"
            elif not view.get("signoff_bound", True):
                rule = "signoff_not_completed"
                message = "submission requires durable exact-content signoff"
            raise DishRuleError(
                code, message, rule=rule,
                details={"action": action, "authoritative_view": view},
            )
        return view

    def _post_operation_view(
        self,
        operation_id: str,
        result: T,
        *,
        schema=None,
    ) -> tuple[T, dict[str, object]]:
        """Return a post-operation view without converting committed success into failure."""
        try:
            return result, self.authoritative_view(operation_id, schema=schema)
        except Exception as exc:
            try:
                op = self.operation(operation_id)
                status = op["status"]
                phase = op["phase"]
            except Exception:
                status = None
                phase = None
            error = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(result, dict):
                result = dict(result)
                result.update({
                    "view_refresh_required": True,
                    "view_refresh_error": error,
                })
            fallback = {
                "status": status,
                "phase": phase,
                "legal_actions": [],
                "recovery_required": False,
                "view_refresh_required": True,
                "view_refresh_error": error,
            }
            return result, fallback

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
        return self._post_operation_view(operation_id, result, schema=schema)

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
        return self.mutate(operation_id, "reopen", executor, schema=schema)

    def recover(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        self.operation(operation_id)
        result = executor()
        return self._post_operation_view(operation_id, result, schema=schema)

    def cancel(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        op = self.operation(operation_id)
        if op["status"] not in {"open", "uncertain"}:
            raise DishRuleError("WRONG_STATE", "operation is not cancellable", rule="operation_not_cancellable")
        result = executor()
        return self._post_operation_view(operation_id, result, schema=schema)

    def authorize_governed_change(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        self.operation(operation_id)
        result = executor()
        return self._post_operation_view(operation_id, result, schema=schema)


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
