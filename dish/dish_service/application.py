"""Transport-neutral shared-service boundary around the existing applications."""
from __future__ import annotations

import contextlib
import inspect
import json
import logging
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_tool.admin import DishAdminApplication
from dish_tool.admin import _OPERATION_TARGET_COMMANDS as _ADMIN_OPERATION_TARGET_COMMANDS
from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication, expose_authoritative_view
from dish_tool.constants import COOKING_PROJECT_GID, SCHEMA_VERSION
from dish_tool.operation_execution import _recover_command_guidance
from dish_tool.database import (
    initialize_database,
    planning_reopen_attempt_by_request,
    process_command_audit_repairs,
    resolve_admin_abandonment_target,
    resolve_admin_operation_target,
    unresolved_planning_reopen_attempts,
)
import dish_tool.database_initialization as database_initialization
from dish_tool.transactions import immediate_transaction, savepoint_transaction
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.invocation_audit import record_invocation_audit
from dish_tool.models import (
    SectionRegistry,
    validate_independence_attestation,
    validate_rejection_reason,
)
from dish_tool.operation_execution import (
    execution_claim_is_live,
    execution_recovery_state,
    live_operation_execution_claim,
)
from dish_tool.step5 import diagnostics_for, start_result_data
from dish_tool.step7 import replay_verification_read
from dish_tool.task_store import (
    planning_reopen_recovery_details,
    planning_reopen_success_data,
    read_complete_task,
    reconcile_planning_reopen_attempt,
)
from dish_tool.releases import resolve_release
from dish_tool.results import error_envelope, result_envelope
from dish_tool.validation_scope import scope_for_command
from dish_tool.transactions import immediate_transaction

from .backup import BackupManager, BackupRecord
from .backup_creation_journal import (
    complete_backup_creation,
    creation_for_request,
    reserve_backup_creation,
)
from .config import ServiceConfig
from .shadow_capture import LegacyShadowCapture, ShadowCaptureSettings
from .identifiers import require_asana_gid, require_dish_uuid
from .leases import LeaseManager, ServicePrincipal
from .maintenance import MaintenanceGate
from .planning_intent import (
    consume_planning_intent,
    issue_or_claim_planning_intent,
    planning_start_may_resume,
)
from .request_replay import begin_request, complete_request, pending_error, stored_result
from .request_coordinators import (
    AdminExecutionState as _AdminExecutionState,
    AdminRequestCoordinator,
    AgentExecutionState as _AgentExecutionState,
    AgentRequestCoordinator,
)
from .restore_fault import RestoreFaultMarker
from .restore_request_journal import RestoreRequestJournal

_READ_ONLY_AGENT_COMMANDS = {"sections", "section-tasks", "read", "inspect"}
_LEASED_AGENT_COMMANDS = {"prepare", "approve", "reject", "submit"}
_MUTATING_AGENT_COMMANDS = {"create", "start", *_LEASED_AGENT_COMMANDS}
_RUN_ID_AGENT_COMMANDS = {"start", "prepare", "approve", "reject"}
_RUN_ID_ADMIN_COMMANDS = {"repair-destination"}
_HANDOFF_PHASES = {"await_verification", "held_evidence", "held_human"}
_OPERATION_ADMIN_COMMANDS = {
    "recover",
    "abandon-operation",
    "reconcile-abandonment",
    "discard",
    "reopen",
    "supply-evidence",
    "record-human-decision",
    "authorize-governed-change",
    "repair-destination",
}
_LEASE_FREE_ADMIN_COMMANDS = {"holds", "authorize-governed-change", "abandon-operation", "reconcile-abandonment"}

LOG = logging.getLogger("dish.service.application")


def _lease_recovery_details(
    operation_id: str, after_recovery_actions: list[str]
) -> dict[str, Any]:
    command = (
        f'dish-admin recover-lease {operation_id} '
        '--reason "<summarize why the lease is being recovered>"'
    )
    next_action = after_recovery_actions[0] if after_recovery_actions else None
    directive = (
        "Tell the human to run the following command after replacing the angle-bracketed "
        "reason text:\n"
        f"{command}\n"
        "Then wait for confirmation it succeeded before continuing — do not start a new "
        "operation; resume this same submission"
        + (f" with `{next_action}`." if next_action else ".")
    )
    return {
        "recovery_required": True,
        "required_admin_action": "recover-lease",
        "resolver": "Marco/admin recover-lease",
        "continuation_surface": "private-admin",
        "connected_action_available": False,
        "admin_command": command,
        "admin_route": f"POST /v1/admin/leases/{operation_id}/recover",
        "directive": directive,
        "legal_next_actions": [],
        "after_recovery": {"legal_actions": list(after_recovery_actions)},
    }


def _exact_uncertain_admin_recovery(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    """Return the one fenced execution whose recovery may use a live lease."""

    rows = conn.execute(
        """SELECT execution_id FROM operation_executions
             WHERE operation_id=? AND status='uncertain' AND resolved_at IS NULL
             ORDER BY created_at, rowid""",
        (operation_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    execution_id = rows[0]["execution_id"]
    if execution_claim_is_live(conn, execution_id=execution_id):
        return None
    recovery = execution_recovery_state(
        conn, execution_id=execution_id, refresh=True
    )
    if not recovery:
        return None
    if (
        not recovery.get("recovery_required")
        or recovery.get("required_admin_action") != "recover"
        or recovery.get("admin_recovery_lease_scope")
        != "exact_uncertain_execution"
    ):
        return None
    return recovery


def _assert_existing_admin_lease_access(
    conn: sqlite3.Connection,
    leases: LeaseManager,
    *,
    command: str,
    operation_id: str,
    principal: ServicePrincipal,
    existing,
) -> dict[str, Any] | None:
    """Enforce a live existing lease without acquiring or transferring it."""

    recovery = (
        _exact_uncertain_admin_recovery(conn, operation_id)
        if command == "recover"
        else None
    )
    if recovery is not None:
        leases.assert_exact_uncertain_recovery(
            operation_id,
            principal,
            execution_id=str(recovery["execution_id"]),
        )
        return recovery
    if leases.is_expired(existing):
        raise DishRuleError(
            "CONFLICT",
            "expired actor lease requires recover-lease first",
            rule="service_lease_expired",
            details={"expires_at": existing["expires_at"]},
        )
    leases.assert_owned(operation_id, principal)
    return None


def _now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _classify_database_initialization_exception(
    exc: BaseException,
) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(exc, DishRuleError):
        details.update({
            "error_classification": "dish_rule_error",
            "original_code": exc.code,
            "original_rule": exc.rule,
            "original_retryable": exc.retryable,
        })
    elif isinstance(exc, sqlite3.Error):
        details["error_classification"] = "sqlite_error"
        error_code = getattr(exc, "sqlite_errorcode", None)
        error_name = getattr(exc, "sqlite_errorname", None)
        if error_code is not None:
            details["sqlite_errorcode"] = error_code
        if error_name is not None:
            details["sqlite_errorname"] = error_name
    elif isinstance(exc, OSError):
        details["error_classification"] = "filesystem_error"
        if exc.errno is not None:
            details["errno"] = exc.errno
    elif isinstance(exc, (TypeError, ValueError, AssertionError)):
        details["error_classification"] = "database_contract_error"
    else:
        details["error_classification"] = "unexpected_error"
    return str(details["error_classification"]), details


def _semantic_evidence_error(
    exc: DishRuleError,
    *,
    execution_occurred: bool,
    request_id_consumed: bool,
) -> DishRuleError:
    """Preserve semantic classification and make retry safety explicit."""

    details = dict(exc.details)
    details.update({
        "execution_occurred": execution_occurred,
        "request_id_consumed": request_id_consumed,
        "retry_condition": (
            "after_database_semantic_evidence_repair_with_fresh_request_id"
            if request_id_consumed
            else "after_database_semantic_evidence_repair"
        ),
    })
    return DishRuleError(
        exc.code,
        str(exc),
        rule=exc.rule,
        retryable=True,
        details=details,
        errors=exc.errors,
    )


def _preserve_semantic_evidence_error(
    exc: DishRuleError,
    *,
    execution_occurred: bool,
    request_id_consumed: bool,
) -> DishRuleError:
    if exc.rule != "database_semantic_evidence_invalid":
        return exc
    return _semantic_evidence_error(
        exc,
        execution_occurred=execution_occurred,
        request_id_consumed=request_id_consumed,
    )


def _preserve_semantic_evidence_result(
    result: dict[str, Any],
    *,
    execution_occurred: bool,
    request_id_consumed: bool,
) -> dict[str, Any]:
    errors = result.get("errors")
    if not isinstance(errors, list) or not any(
        isinstance(error, dict)
        and error.get("rule") == "database_semantic_evidence_invalid"
        for error in errors
    ):
        return result
    retry_condition = (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
        if request_id_consumed
        else "after_database_semantic_evidence_repair"
    )
    for error in errors:
        if (
            isinstance(error, dict)
            and error.get("rule") == "database_semantic_evidence_invalid"
        ):
            error.update({
                "execution_occurred": execution_occurred,
                "request_id_consumed": request_id_consumed,
                "retry_condition": retry_condition,
            })
    result["retryable"] = True
    return result


def _database_initialization_error(exc: BaseException) -> DishRuleError:
    if (
        isinstance(exc, DishRuleError)
        and exc.rule == "database_semantic_evidence_invalid"
    ):
        return _semantic_evidence_error(
            exc,
            execution_occurred=False,
            request_id_consumed=False,
        )

    _classification, details = _classify_database_initialization_exception(exc)
    details.update({
        "execution_occurred": False,
        "request_id_consumed": False,
        "retry_condition": "after_database_availability_restored",
    })
    return DishRuleError(
        "INTERNAL_ERROR",
        "Dish database is unavailable; the request was not executed",
        rule="service_database_unavailable",
        retryable=True,
        details=details,
    )


def _database_execution_unavailable_error(
    exc: BaseException,
    *,
    request_id_consumed: bool,
) -> DishRuleError:
    """Report post-start database failures without implying a safe blind retry."""

    if isinstance(exc, DishRuleError):
        preserved = _preserve_semantic_evidence_error(
            exc,
            execution_occurred=True,
            request_id_consumed=request_id_consumed,
        )
        if preserved is not exc:
            return preserved

    _classification, details = _classify_database_initialization_exception(exc)
    details.update({
        "execution_occurred": True,
        "request_id_consumed": request_id_consumed,
        "retry_condition": "reconcile_request_state_before_retry",
    })
    return DishRuleError(
        "INTERNAL_ERROR",
        "Dish database became unavailable after request execution began; "
        "reconcile request state before retrying",
        rule="service_database_unavailable",
        retryable=False,
        details=details,
    )


def _probe_database_write_readiness(conn: sqlite3.Connection) -> None:
    """Prove bounded main-database write capability without committing state."""

    timeout_row = conn.execute("PRAGMA busy_timeout").fetchone()
    prior_timeout_ms = int(timeout_row[0]) if timeout_row is not None else 0
    try:
        conn.execute("PRAGMA busy_timeout = 100")
        with savepoint_transaction(conn, "health_write_probe"):
            conn.execute(
                """UPDATE schema_migrations
                      SET applied_at = applied_at
                    WHERE version = (SELECT MAX(version) FROM schema_migrations)"""
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "workflow database readiness probe could not bind the current schema",
                    rule="database_write_probe_invalid",
                    retryable=False,
                )
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        error_code = getattr(exc, "sqlite_errorcode", None)
        primary_code = None if error_code is None else error_code & 0xFF
        if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or (
            "locked" in message or "busy" in message
        ):
            raise DishRuleError(
                "BACKEND_REJECTED",
                "workflow database write readiness is temporarily blocked",
                rule="database_writer_lock",
                retryable=True,
                details={"timeout_ms": 100},
            ) from exc
        if primary_code == sqlite3.SQLITE_READONLY or any(
            marker in message
            for marker in ("readonly", "read-only", "permission denied")
        ):
            raise DishRuleError(
                "VALIDATION_FAILED",
                "workflow database is not mutation-ready",
                rule="database_not_writable",
                retryable=False,
            ) from exc
        raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {prior_timeout_ms}")


class DishService:
    """Shared persistent authority around the existing workflow applications.

    Every request gets a fresh SQLite connection. Ordinary requests may run
    concurrently, while an in-process maintenance gate gives database replacement
    exclusive access. Durable operation constraints and service leases remain the
    cross-request workflow authority. An explicitly supplied backend_factory and
    every resource it creates remain caller-owned; only the default internally
    selected backend factory produces service-owned instances that Dish closes.
    """

    def __init__(
        self,
        config: ServiceConfig,
        *,
        backend_factory: Callable[[], Any] | None = None,
        release_loader: Callable[..., Any] | None = None,
        lease_now=None,
    ) -> None:
        self.config = config
        self._owns_backend_instances = backend_factory is None
        self.backend_factory = backend_factory or AsanaBackend
        self.release_loader = release_loader
        self.lease_now = lease_now
        self._maintenance_gate = MaintenanceGate()
        self._planning_intent_locks = tuple(threading.Lock() for _ in range(64))
        self._restore_fault = RestoreFaultMarker(self.config.db_path)
        self._restore_requests = RestoreRequestJournal(self.config.db_path)
        self._agent_requests = AgentRequestCoordinator(
            self, initialization_error=_database_initialization_error
        )
        self._admin_requests = AdminRequestCoordinator(
            self, initialization_error=_database_initialization_error
        )
        self._shadow_capture = LegacyShadowCapture(
            ShadowCaptureSettings(
                mode=config.dark_launch_mode,
                spool_path=config.dark_launch_spool_path or config.db_path.with_name("dark-launch-spool.sqlite3"),
                emergency_dir=config.dark_launch_emergency_dir or config.db_path.with_name("dark-launch-emergency"),
                source_authority_generation=config.dark_launch_source_generation,
                kill_switch_path=config.dark_launch_kill_switch_path,
                busy_timeout_ms=config.dark_launch_busy_timeout_ms,
            ),
            db_path=config.db_path,
        )

    def _planning_intent_execution_lock(
        self, command: str, arguments: Mapping[str, Any]
    ):
        if command != "start" or arguments.get("kind") != "planning":
            return contextlib.nullcontext()
        challenge_id = arguments.get("intent_challenge_id")
        if not isinstance(challenge_id, str) or not challenge_id:
            return contextlib.nullcontext()
        index = sum(challenge_id.encode("utf-8")) % len(self._planning_intent_locks)
        return self._planning_intent_locks[index]

    def _initialize_database(
        self,
        *,
        surface: str,
        command: str | None = None,
        request_id: str | None = None,
        principal: ServicePrincipal | None = None,
        task_gid: str | None = None,
        operation_id: str | None = None,
        backup_id: str | None = None,
    ) -> sqlite3.Connection:
        """Initialize SQLite and retain safe diagnostics for every failure."""

        try:
            if surface in {"startup", "health", "admin"}:
                return initialize_database(self.config.db_path)
            return database_initialization.open_runtime_database(self.config.db_path)
        except Exception as exc:
            classification, _details = _classify_database_initialization_exception(exc)
            context = {
                key: value
                for key, value in {
                    "surface": surface,
                    "command": command,
                    "request_id": request_id,
                    "owner_id": None if principal is None else principal.owner_id,
                    "run_id": None if principal is None else principal.run_id,
                    "task_gid": task_gid,
                    "operation_id": operation_id,
                    "backup_id": backup_id,
                }.items()
                if value not in {None, ""}
            }
            # Only explicitly selected identifiers are logged. Never serialize
            # command arguments, candidate text, rejection reasons, or tokens.
            LOG.error(
                "database_initialization_failed classification=%s error_type=%s context=%s",
                classification,
                type(exc).__name__,
                json.dumps(context, sort_keys=True, separators=(",", ":")),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise

    @property
    def backup_manager(self) -> BackupManager:
        backup_dir = self.config.backup_dir or (self.config.db_path.parent / "backups")
        return BackupManager(self.config.db_path, backup_dir)

    def _release(self, role: str | None = None, *, include_migrations: bool = False):
        if self.release_loader is not None:
            loader = self.release_loader
            candidates = (
                ((role,), {"include_migrations": include_migrations}),
                ((role,), {}),
                ((), {}),
            )
            try:
                signature = inspect.signature(loader)
            except (TypeError, ValueError):
                # Opaque callables get the full current contract exactly once.
                return loader(role, include_migrations=include_migrations)
            for args, kwargs in candidates:
                try:
                    signature.bind(*args, **kwargs)
                except TypeError:
                    continue
                return loader(*args, **kwargs)
            raise DishRuleError(
                "INTERNAL_ERROR",
                "release loader has an unsupported call signature",
                rule="release_loader_signature_unsupported",
            )
        return resolve_release(
            self.config.honest_root,
            protocol_role=role,
            include_migrations=include_migrations,
        )

    def _lease_manager(self, conn):
        kwargs = {"ttl_seconds": self.config.lease_ttl_seconds}
        if self.lease_now is not None:
            kwargs["now"] = self.lease_now
        return LeaseManager(conn, **kwargs)

    def _close_backend(self, backend: Any | None) -> None:
        if backend is None or not self._owns_backend_instances:
            return
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                # Cleanup occurs after command authority may already be committed.
                # Log it, but never replace the command result or skip DB closure.
                LOG.warning(
                    "backend_cleanup_failed error_type=%s",
                    type(exc).__name__,
                )

    @staticmethod
    def _default_principal(arguments: Mapping[str, Any], *, admin: bool = False) -> ServicePrincipal:
        agent = str(arguments.get("agent") or ("marco-admin" if admin else "local")).strip() or "local"
        prefix = "admin" if admin else "local"
        run = str(arguments.get("run_id") or f"{prefix}:{agent}").strip()
        return ServicePrincipal(owner_id=f"{prefix}:{agent}", run_id=run)

    @contextlib.contextmanager
    def _candidate_file(self, arguments: Mapping[str, Any]):
        prepared = dict(arguments)
        text = prepared.pop("file_text", None)
        if text is None:
            yield prepared
            return
        if "file_path" in prepared:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "provide file_text or file_path, not both",
                rule="candidate_transport_conflict",
            )
        if not isinstance(text, str):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "file_text must be a string",
                rule="candidate_text_invalid",
            )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(text)
            path = Path(handle.name)
        prepared["file_path"] = str(path)
        try:
            yield prepared
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _operation_for_request(conn, command: str, arguments: Mapping[str, Any]):
        operation_id = str(arguments.get("submission_id") or "").strip()
        if operation_id:
            return operation_id
        if command == "start" and arguments.get("prepared_operation_id"):
            return str(arguments.get("prepared_operation_id") or "").strip() or None
        if command == "start" and arguments.get("target_operation_id"):
            return str(arguments.get("target_operation_id") or "").strip() or None
        if command == "start" and arguments.get("kind") == "verification":
            task_gid = str(arguments.get("task_gid") or "").strip()
            row = conn.execute(
                "SELECT operation_id FROM operations WHERE task_gid=? AND status IN ('open','uncertain') ORDER BY created_at DESC LIMIT 1",
                (task_gid,),
            ).fetchone()
            return None if row is None else row["operation_id"]
        return None

    @staticmethod
    def _arguments_for_principal(
        command: str,
        arguments: Mapping[str, Any],
        *,
        run_id: str | None,
    ) -> dict[str, Any]:
        prepared = dict(arguments)
        if command == "start" and prepared.get("kind") != "planning":
            for field in ("intent_challenge_id", "intent_basis", "override_reason"):
                if field in prepared:
                    raise DishRuleError(
                        "INVALID_ARGUMENT",
                        f"{field} is accepted only for Planning starts",
                        rule="argument_unexpected",
                        retryable=False,
                        details={"field": field},
                    )
        if prepared.get("independence_attestation") is not None:
            prepared["independence_attestation"] = validate_independence_attestation(
                prepared["independence_attestation"]
            )
        if command not in _RUN_ID_AGENT_COMMANDS or run_id is None:
            return prepared
        supplied = str(prepared.get("run_id") or "").strip()
        if supplied and supplied != run_id:
            raise DishRuleError(
                "AGENT_MISMATCH",
                "command run identity conflicts with the authenticated client run",
                rule="service_run_id_conflict",
                details={"client_run_id": run_id, "command_run_id": supplied},
            )
        prepared["run_id"] = run_id
        return prepared

    @staticmethod
    def _verification_lease_cycle_id(
        conn,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        pending_first: bool = False,
    ) -> str:
        if not pending_first:
            bound = conn.execute(
                """SELECT cycle_id FROM verification_cycles
                     WHERE operation_id=? AND run_id=?
                     ORDER BY cycle_number DESC LIMIT 1""",
                (operation_id, principal.run_id),
            ).fetchone()
            if bound is not None:
                return str(bound["cycle_id"])
        pending = conn.execute(
            """SELECT cycle_id FROM verification_cycles
                 WHERE operation_id=? AND completed_at IS NULL
                 ORDER BY cycle_number DESC LIMIT 2""",
            (operation_id,),
        ).fetchall()
        if len(pending) != 1:
            raise DishRuleError(
                "WRONG_STATE",
                "Verification lease requires one exact current cycle",
                rule="verification_cycle_context_required",
                details={"operation_id": operation_id, "candidate_count": len(pending)},
            )
        return str(pending[0]["cycle_id"])

    @staticmethod
    def _lease_payload(row) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "lease_id": row["lease_id"],
            "operation_id": row["operation_id"],
            "owner_id": row["owner_id"],
            "run_id": row["run_id"],
            "acquired_at": row["acquired_at"],
            "renewed_at": row["renewed_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _lease_expiry_payload(row) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "lease_id": row["lease_id"],
            "operation_id": row["operation_id"],
            "task_gid": row["task_gid"],
            "owner_id": row["owner_id"],
            "run_id": row["run_id"],
            "acquired_at": row["acquired_at"],
            "renewed_at": row["renewed_at"],
            "expires_at": row["expires_at"],
            "released_at": row["released_at"],
            "release_reason": row["release_reason"],
        }

    @staticmethod
    def _synchronize_exposed_actions(
        result: dict[str, Any], actions: list[str], *, ensure_legal_next: bool = False
    ) -> None:
        result["allowed_actions"] = list(actions)
        data = result.setdefault("data", {})
        if ensure_legal_next or "legal_next_actions" in data:
            data["legal_next_actions"] = list(actions)
        authoritative_view = data.get("authoritative_view")
        if isinstance(authoritative_view, dict):
            authoritative_view["legal_actions"] = list(actions)
        active_operation = data.get("active_operation")
        if isinstance(active_operation, dict):
            active_view = active_operation.get("authoritative_view")
            if isinstance(active_view, dict):
                active_view["legal_actions"] = list(actions)

    def _exposed_operation_view(
        self,
        conn,
        operation_id: str,
        *,
        app: DishApplication | None = None,
    ) -> dict[str, Any] | None:
        owned_backend = None
        try:
            if app is None:
                owned_backend = self.backend_factory()
                app = DishApplication(
                    conn,
                    owned_backend,
                    release_loader=lambda role=None: self._release(role),
                )
            release = self._release(None)
            return expose_authoritative_view(
                app.operation_service.authoritative_view(
                    operation_id, schema=release.schema
                )
            )
        except Exception as exc:
            LOG.warning(
                "lease_recovery_view_unavailable operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return None
        finally:
            self._close_backend(owned_backend)

    def _apply_expired_lease_guidance(
        self,
        result: dict[str, Any],
        *,
        operation_id: str,
        after_recovery_actions: list[str],
        authoritative_view: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = result.setdefault("data", {})
        if authoritative_view is not None:
            data["authoritative_view"] = dict(authoritative_view)
        self._synchronize_exposed_actions(
            result, [], ensure_legal_next=True
        )
        guidance = _lease_recovery_details(
            operation_id, after_recovery_actions
        )
        data.update(guidance)
        access = data.setdefault("service_access", {})
        access.update({
            "state": "expired",
            "rule": "service_lease_expired",
            "required_admin_action": "recover-lease",
            "resolver": guidance["resolver"],
            "continuation_surface": guidance["continuation_surface"],
            "connected_action_available": guidance["connected_action_available"],
            "admin_command": guidance["admin_command"],
            "admin_route": guidance["admin_route"],
            "after_recovery": guidance["after_recovery"],
        })
        return result

    @staticmethod
    def _operation_row(conn, operation_id: str):
        return conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()

    @staticmethod
    def _planning_reopen_result(
        attempt: Mapping[str, Any],
        *,
        state: str,
        live=None,
    ) -> dict[str, Any]:
        if state == "confirmed":
            return result_envelope(
                command="reopen-planning",
                task_gid=attempt["task_gid"],
                allowed_actions=["start"],
                data=planning_reopen_success_data(attempt, live=live),
            )
        if state == "not_applied":
            return error_envelope(
                "reopen-planning",
                BackendFailure(
                    "BACKEND_REJECTED",
                    "task completion state was not reopened",
                    rule="planning_reopen_not_applied",
                    retryable=True,
                    details={
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                    },
                ),
                task_gid=attempt["task_gid"],
            )
        raise ValueError(f"cannot build terminal Planning reopen result for {state}")

    @staticmethod
    def _ensure_planning_reopen_invocation_audit(
        conn, *, attempt: Mapping[str, Any], result: dict[str, Any]
    ) -> None:
        request_id = str(attempt["request_id"] or "").strip() or None
        if request_id is None:
            return
        existing = conn.execute(
            """SELECT event_id FROM audit_events
                 WHERE event_type='dish-admin.reopen-planning'
                   AND json_extract(details, '$.request_id')=?
                 LIMIT 1""",
            (request_id,),
        ).fetchone()
        if existing is not None:
            return
        record_invocation_audit(
            conn,
            surface="dish-admin",
            command="reopen-planning",
            result=result,
            task_gid=attempt["task_gid"],
            submission_id=None,
            actor_role="marco",
            actor_run_id=attempt["actor_run_id"],
            audit_details={
                "attempt_id": attempt["attempt_id"],
                "request_id": request_id,
                "reason": attempt["reason"],
                "completed_before": True,
                "completed_after": result.get("ok") is True,
                "identity": attempt["expected_identity"],
                "section_gid": attempt["expected_section_gid"],
                "reconciled": True,
            },
        )

    def _complete_planning_reopen_request(
        self,
        *,
        conn,
        attempt: Mapping[str, Any],
        state: str,
        live=None,
    ) -> dict[str, Any]:
        result = self._planning_reopen_result(attempt, state=state, live=live)
        request_id = str(attempt["request_id"] or "").strip() or None
        with immediate_transaction(conn, "planning_reopen_request_completion"):
            self._ensure_planning_reopen_invocation_audit(
                conn, attempt=attempt, result=result
            )
            if request_id:
                result.setdefault("data", {})["request_id"] = request_id
                complete_request(conn, request_id=request_id, result=result)
        return result

    def _complete_terminal_planning_reopen_request(
        self, *, conn, request_id: str
    ) -> dict[str, Any] | None:
        attempt = planning_reopen_attempt_by_request(conn, request_id=request_id)
        if attempt is None or attempt["outcome"] not in {"confirmed", "not_applied"}:
            return None
        return self._complete_planning_reopen_request(
            conn=conn, attempt=attempt, state=attempt["outcome"], live=None
        )

    def _reconcile_pending_planning_reopen_request(
        self,
        *,
        conn,
        backend,
        request_id: str,
    ) -> dict[str, Any] | None:
        attempt = planning_reopen_attempt_by_request(conn, request_id=request_id)
        if attempt is None:
            # The process died after request journaling but before durable effect
            # intent. The original request may resume through the normal handler.
            return None
        try:
            recovery = reconcile_planning_reopen_attempt(
                conn,
                backend,
                attempt_id=attempt["attempt_id"],
                project_gid=COOKING_PROJECT_GID,
                allow_external_retry=True,
                recovered_by="exact_request_replay",
            )
        except BackendFailure as exc:
            result = error_envelope(
                "reopen-planning", exc, task_gid=attempt["task_gid"]
            )
            result.setdefault("data", {}).update(exc.details)
            result["data"]["request_id"] = request_id
            # An unresolved Planning reopen remains pending so a later exact
            # replay can converge it when live evidence becomes authoritative.
            if exc.code != "BACKEND_UNCERTAIN":
                with immediate_transaction(
                    conn, "planning_reopen_failed_request_completion"
                ):
                    self._ensure_planning_reopen_invocation_audit(
                        conn, attempt=attempt, result=result
                    )
                    complete_request(conn, request_id=request_id, result=result)
            return result
        state = recovery["state"]
        return self._complete_planning_reopen_request(
            conn=conn,
            attempt=recovery["attempt"],
            state=state,
            live=recovery["live"],
        )

    def _reconcile_planning_reopens_startup(self, conn) -> dict[str, Any]:
        attempts = unresolved_planning_reopen_attempts(conn)
        summary: dict[str, Any] = {
            "discovered": len(attempts),
            "confirmed": 0,
            "not_applied": 0,
            "resume_safe": 0,
            "applied_pending_replay": 0,
            "uncertain": 0,
            "pending": [],
            "errors": [],
        }
        if not attempts:
            return summary

        live_attempts = []
        for attempt in attempts:
            if attempt["outcome"] in {"confirmed", "not_applied"}:
                try:
                    self._complete_planning_reopen_request(
                        conn=conn, attempt=attempt, state=attempt["outcome"], live=None
                    )
                    summary[attempt["outcome"]] += 1
                except Exception as exc:
                    summary["errors"].append({
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                        "error_type": type(exc).__name__,
                    })
            else:
                live_attempts.append(attempt)
        if not live_attempts:
            return summary

        try:
            backend = self.backend_factory()
        except Exception as exc:
            summary["errors"].append({"error_type": type(exc).__name__})
            summary["uncertain"] += len(live_attempts)
            for attempt in live_attempts:
                details = planning_reopen_recovery_details(attempt)
                details.update({
                    "observed_state": "backend_unavailable",
                    "error_type": type(exc).__name__,
                })
                summary["pending"].append(details)
            return summary
        try:
            for attempt in live_attempts:
                try:
                    recovery = reconcile_planning_reopen_attempt(
                        conn,
                        backend,
                        attempt_id=attempt["attempt_id"],
                        project_gid=COOKING_PROJECT_GID,
                        allow_external_retry=False,
                        recovered_by="service_startup",
                        finalize_observed_applied=False,
                    )
                    state = recovery["state"]
                    summary[state] += 1
                    if state in {"confirmed", "not_applied"}:
                        self._complete_planning_reopen_request(
                            conn=conn,
                            attempt=recovery["attempt"],
                            state=state,
                            live=recovery["live"],
                        )
                    else:
                        recovery_attempt = dict(recovery["attempt"])
                        recovery_attempt["request_status"] = attempt["request_status"]
                        details = planning_reopen_recovery_details(
                            recovery_attempt,
                            safe_to_resume=state == "resume_safe",
                        )
                        details["observed_state"] = state
                        summary["pending"].append(details)
                except BackendFailure as exc:
                    summary["uncertain"] += 1
                    details = planning_reopen_recovery_details(attempt)
                    details.update({
                        "observed_state": "uncertain",
                        "code": exc.code,
                        "rule": exc.rule,
                    })
                    summary["pending"].append(details)
                    summary["errors"].append({
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                        "code": exc.code,
                        "rule": exc.rule,
                    })
                except Exception as exc:
                    summary["uncertain"] += 1
                    details = planning_reopen_recovery_details(attempt)
                    details.update({
                        "observed_state": "reconciliation_error",
                        "error_type": type(exc).__name__,
                    })
                    summary["pending"].append(details)
                    summary["errors"].append({
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                        "error_type": type(exc).__name__,
                    })
        finally:
            self._close_backend(backend)
        return summary

    def _reconcile_pending_operation_request(
        self,
        *,
        conn,
        command: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        """Return durable recovery guidance before constructing a backend.

        A dead execution with no observed effects may resume through the normal
        command path. Active, completed-without-result, and partial executions
        remain fail-closed.
        """
        recovery = execution_recovery_state(
            conn, request_id=request_id, include_completed=True
        )
        if recovery is None:
            raise pending_error(command, request_id)
        if execution_claim_is_live(conn, execution_id=recovery["execution_id"]):
            raise pending_error(command, request_id)
        if not (
            recovery.get("result_persistence_missing")
            or recovery["recovery_required"]
        ):
            return None

        operation_id = recovery["operation_id"]
        operation = self._operation_row(conn, operation_id)
        if recovery.get("result_persistence_missing"):
            message = (
                "the mutation completed durably but its response envelope was "
                "not persisted"
            )
            rule = "service_request_result_missing"
        else:
            message = (
                "an earlier operation execution has durable effects; do not "
                "repeat the mutation"
            )
            rule = "service_request_pending"
        result = error_envelope(
            command,
            DishRuleError(
                "BACKEND_UNCERTAIN",
                message,
                rule=rule,
                retryable=False,
                details=recovery,
            ),
            task_gid=None if operation is None else operation["task_gid"],
            submission_id=operation_id,
            state=None if operation is None else operation["status"],
        )
        result.setdefault("data", {}).update(recovery)
        result["data"]["request_id"] = request_id
        complete_request(conn, request_id=request_id, result=result)
        return result

    @staticmethod
    def _run_has_role(conn, operation_id: str, run_id: str, roles: tuple[str, ...]) -> bool:
        placeholders = ",".join("?" for _ in roles)
        row = conn.execute(
            f"SELECT 1 FROM operation_actor_facts "
            f"WHERE operation_id=? AND run_id=? AND role IN ({placeholders}) LIMIT 1",
            (operation_id, run_id, *roles),
        ).fetchone()
        return row is not None

    @staticmethod
    def _is_preconstruction_research_reject(op, command: str) -> bool:
        return bool(
            command == "reject"
            and op["operation_kind"] == "initial"
            and op["phase"] == "prepare_required"
            and op["content_write_completed_at"] is None
        )

    def _stage_actor_may_claim_missing_lease(
        self,
        conn,
        operation_id: str,
        principal: ServicePrincipal,
        op,
        *,
        agent: str | None = None,
    ) -> bool:
        expected_agent = (
            op["researcher_agent"]
            if op["operation_kind"] == "initial"
            else op["editor_agent"]
        )
        if agent and expected_agent and agent != expected_agent:
            return False
        if str(op["run_id"] or "").strip() == principal.run_id:
            return True
        return self._run_has_role(
            conn,
            operation_id,
            principal.run_id,
            ("planner", "constructor", "material_editor"),
        )

    @staticmethod
    def _active_abandonment_for_operation(conn, operation_id: str):
        return conn.execute(
            """SELECT abandonment.*, succession.successor_operation_id AS linked_successor_id
                 FROM abandonment_attempts AS abandonment
                 LEFT JOIN operation_successions AS succession
                   ON succession.abandonment_id=abandonment.abandonment_id
                WHERE abandonment.status!='completed'
                  AND (abandonment.source_operation_id=?
                       OR succession.successor_operation_id=?)
                ORDER BY abandonment.created_at DESC LIMIT 1""",
            (operation_id, operation_id),
        ).fetchone()

    @staticmethod
    def _active_abandonment_for_task(conn, task_gid: str):
        return conn.execute(
            """SELECT * FROM abandonment_attempts
                WHERE task_gid=? AND status!='completed'
                ORDER BY created_at DESC LIMIT 1""",
            (task_gid,),
        ).fetchone()

    def _assert_connected_task_abandonment_access(
        self,
        conn,
        *,
        command: str,
        arguments: Mapping[str, Any],
    ) -> None:
        if command != "start":
            return
        task_gid = str(arguments.get("task_gid") or "").strip()
        if not task_gid:
            return
        abandonment = self._active_abandonment_for_task(conn, task_gid)
        if abandonment is None:
            return
        ready_to_claim = bool(
            abandonment["status"] == "awaiting_successor_claim"
            and abandonment["successor_operation_id"]
        )
        if ready_to_claim:
            # A successor is prepared and ready. Whether the caller names no
            # target, the exact current target, or a stale one, ordinary start
            # handling resolves and validates the exact target itself further
            # down (see resolve_verification_start_target and
            # claim_prepared_stage_successor) and reports a self-correctable
            # error there. Only a still-blocked abandonment needs the human to
            # run admin reconciliation.
            return
        command_text = (
            f'dish-admin reconcile-abandonment {abandonment["abandonment_id"]}'
        )
        raise DishRuleError(
            "WRONG_STATE",
            "task is fenced by an active permanent-run abandonment",
            rule="abandonment_fence_active",
            details={
                "abandonment_id": abandonment["abandonment_id"],
                "abandonment_status": abandonment["status"],
                "required_admin_action": "reconcile-abandonment",
                "admin_command": command_text,
                "directive": (
                    f"Tell the human to run: {command_text}\n"
                    "Then wait for confirmation it succeeded and refresh the "
                    "authoritative Dish action."
                ),
            },
        )

    def _assert_connected_abandonment_access(
        self,
        conn,
        *,
        command: str,
        arguments: Mapping[str, Any],
        operation_id: str,
    ) -> None:
        abandonment = self._active_abandonment_for_operation(conn, operation_id)
        if abandonment is None:
            return
        prepared_claim = bool(
            command == "start"
            and abandonment["status"] == "awaiting_successor_claim"
            and abandonment["successor_operation_id"] == operation_id
        )
        if prepared_claim:
            # By this point operation_id was already resolved and validated
            # against the abandonment's exact prepared successor (see
            # resolve_verification_start_target and _resolve_agent_operation),
            # so no further argument echo is required here.
            return
        command_text = (
            f'dish-admin reconcile-abandonment {abandonment["abandonment_id"]}'
        )
        raise DishRuleError(
            "WRONG_STATE",
            "task is fenced by an active permanent-run abandonment",
            rule="abandonment_fence_active",
            details={
                "abandonment_id": abandonment["abandonment_id"],
                "abandonment_status": abandonment["status"],
                "required_admin_action": "reconcile-abandonment",
                "admin_command": command_text,
                "directive": (
                    f"Tell the human to run: {command_text}\n"
                    "Then wait for confirmation it succeeded and refresh the "
                    "authoritative Dish action."
                ),
            },
        )

    def _may_claim_missing_lease(
        self,
        conn,
        operation_id: str,
        principal: ServicePrincipal,
        command: str,
        *,
        agent: str | None = None,
    ) -> bool:
        op = self._operation_row(conn, operation_id)
        if op is None or op["status"] != "open":
            return False
        if command == "prepare" or self._is_preconstruction_research_reject(
            op, command
        ):
            return self._stage_actor_may_claim_missing_lease(
                conn, operation_id, principal, op, agent=agent
            )
        if command in {"approve", "reject", "submit"}:
            if agent and op["verifier_agent"] and agent != op["verifier_agent"]:
                return False
            return self._run_has_role(
                conn, operation_id, principal.run_id, ("verifier",)
            )
        return False

    def _reclaimed_lease_cycle_id(
        self,
        conn,
        operation_id: str,
        principal: ServicePrincipal,
        command: str,
    ) -> str | None:
        if command == "prepare":
            return None
        op = self._operation_row(conn, operation_id)
        if op is not None and self._is_preconstruction_research_reject(op, command):
            return None
        return self._verification_lease_cycle_id(conn, operation_id, principal)

    def _apply_principal_access(
        self,
        result: dict[str, Any],
        *,
        conn,
        leases: LeaseManager,
        operation_id: str | None,
        principal: ServicePrincipal,
        agent: str | None = None,
    ) -> dict[str, Any]:
        if not operation_id:
            return result
        op = self._operation_row(conn, operation_id)
        if op is None:
            return result
        data = result.setdefault("data", {})
        active = leases.active_for_operation(operation_id)
        data["service_lease"] = self._lease_payload(active)
        actions = list(result.get("allowed_actions") or [])
        after_recovery_actions: list[str] = []

        access: dict[str, Any] = {"state": "available"}
        if op["status"] == "uncertain":
            actions = []
            access = {
                "state": "recovery_required",
                "rule": "operation_uncertain",
                "required_admin_action": "recover",
            }
            data["recovery_required"] = True
        elif op["status"] != "open":
            terminal_handoff = (
                actions == ["start"]
                and data.get("required_start_kind") in {"initial", "verification"}
            )
            if terminal_handoff:
                access = {"state": "handoff"}
            else:
                actions = []
                access = {"state": "terminal"}
        elif active is not None:
            if leases.is_expired(active):
                after_recovery_actions = list(actions)
                actions = []
                access = {
                    "state": "expired",
                    "rule": "service_lease_expired",
                    "required_admin_action": "recover-lease",
                }
                data["recovery_required"] = True
            elif leases.is_owned_by(active, principal):
                access = {"state": "owned"}
            else:
                actions = []
                access = {
                    "state": "held_by_other_run",
                    "rule": "service_lease_owner_mismatch",
                    "owner_id": active["owner_id"],
                    "run_id": active["run_id"],
                    "expires_at": active["expires_at"],
                }
        else:
            filtered: list[str] = []
            for action in actions:
                if action == "start":
                    filtered.append(action)
                elif action in _LEASED_AGENT_COMMANDS and self._may_claim_missing_lease(
                    conn, operation_id, principal, action, agent=agent
                ):
                    filtered.append(action)
            actions = filtered
            if actions:
                access = {"state": "claimable_by_run"}
            elif op["phase"] in _HANDOFF_PHASES:
                access = {"state": "handoff"}
            else:
                access = {
                    "state": "recovery_required",
                    "rule": "service_lease_missing",
                    "required_admin_action": "recover-lease",
                }
                data["recovery_required"] = True

        self._synchronize_exposed_actions(result, actions)
        data["service_access"] = access
        workflow_required_admin_action = data.get("required_admin_action")
        required_admin_action = access.get("required_admin_action")
        if required_admin_action:
            data["required_admin_action"] = required_admin_action
        elif workflow_required_admin_action is None:
            data.pop("required_admin_action", None)
        if access.get("rule") == "service_lease_expired":
            return self._apply_expired_lease_guidance(
                result,
                operation_id=operation_id,
                after_recovery_actions=after_recovery_actions,
            )
        if access.get("rule") == "operation_uncertain" and not data.get("admin_command"):
            data.update(_recover_command_guidance(operation_id))
        return result

    def _assert_mutation_ready(self, backend: Any) -> None:
        # Compatibility is resolved before any workflow mutation. Asana access is
        # proven with the same read-only section registry contract used by the
        # workflow itself; malformed/missing queues fail closed. A restore whose
        # rollback could not be proven keeps this process diagnosis-only.
        if self._restore_fault.active():
            raise DishRuleError(
                "INTERNAL_ERROR",
                "database restore recovery is incomplete; workflow mutations are disabled",
                rule="service_restore_recovery_required",
            )
        self._release(None)
        SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))

    def _finalize_successful_lease(
        self,
        *,
        result: dict[str, Any],
        conn,
        leases: LeaseManager,
        operation_id: str,
        principal: ServicePrincipal,
        command: str,
        admin: bool = False,
        exact_recovery_execution_id: str | None = None,
        exact_recovery_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply post-success lease bookkeeping without reversing success.

        The workflow application may already have committed Asana and database
        effects. A later lease-release/read failure must therefore suppress
        follow-on actions and require service recovery, not turn the completed
        mutation into a retryable command failure.
        """
        try:
            op = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if op is not None:
                if (
                    admin
                    and command == "recover"
                    and exact_recovery_execution_id is not None
                    and exact_recovery_lease_id is not None
                    and op["status"] == "open"
                    and op["phase"] in _HANDOFF_PHASES
                ):
                    leases.release_after_exact_recovery_handoff(
                        operation_id,
                        execution_id=exact_recovery_execution_id,
                        lease_id=exact_recovery_lease_id,
                        reason=(
                            "exact_recovery_handoff:"
                            f"{exact_recovery_execution_id}:{op['phase']}"
                        ),
                    )
                elif op["status"] in {"completed", "cancelled"}:
                    leases.release_terminal(
                        operation_id,
                        principal,
                        reason="admin_operation_terminal" if admin else "operation_terminal",
                    )
                elif not admin and op["phase"] in _HANDOFF_PHASES and command in {"prepare", "reject"}:
                    leases.release_for_handoff(
                        operation_id,
                        principal,
                        reason=f"workflow_handoff:{op['phase']}",
                    )
            active = leases.active_for_operation(operation_id)
            result.setdefault("data", {})["service_lease"] = self._lease_payload(active)
        except Exception as exc:
            data = result.setdefault("data", {})
            exact_handoff_release = bool(
                admin
                and command == "recover"
                and exact_recovery_execution_id is not None
                and exact_recovery_lease_id is not None
            )
            if exact_handoff_release:
                data["service_recovery_required"] = True
                data["service_recovery"] = {
                    "kind": "exact_recovery_handoff_lease_release",
                    "operation_id": operation_id,
                    "execution_id": exact_recovery_execution_id,
                    "lease_id": exact_recovery_lease_id,
                    "command": command,
                    "error_type": type(exc).__name__,
                    "do_not_retry_command": True,
                }
                try:
                    data["service_lease"] = self._lease_payload(
                        leases.active_for_operation(operation_id)
                    )
                except Exception as lease_read_exc:
                    data["service_lease"] = None
                    data["service_recovery"]["lease_read_error_type"] = type(
                        lease_read_exc
                    ).__name__
                self._synchronize_exposed_actions(result, [])
                result["retryable"] = False
                return result
            fallback_error = None
            try:
                op = conn.execute(
                    "SELECT status,phase FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                active = leases.active_for_operation(operation_id)
                should_release = bool(
                    active is not None
                    and op is not None
                    and (
                        op["status"] in {"completed", "cancelled"}
                        or (
                            not admin
                            and op["phase"] in _HANDOFF_PHASES
                            and command in {"prepare", "reject"}
                        )
                    )
                )
                if should_release:
                    leases.release(
                        operation_id,
                        None,
                        reason=f"lease_finalization_fallback:{command}",
                        admin=True,
                    )
                data["service_lease"] = self._lease_payload(
                    leases.active_for_operation(operation_id)
                )
                data["service_cleanup_warning"] = {
                    "kind": "lease_finalization",
                    "operation_id": operation_id,
                    "command": command,
                    "error_type": type(exc).__name__,
                    "fallback_release_applied": should_release,
                }
            except Exception as fallback_exc:
                fallback_error = fallback_exc
            if fallback_error is not None:
                data["service_recovery_required"] = True
                data["service_recovery"] = {
                    "kind": "lease_finalization",
                    "operation_id": operation_id,
                    "command": command,
                    "error_type": type(exc).__name__,
                    "fallback_error_type": type(fallback_error).__name__,
                    "do_not_retry_command": True,
                }
                try:
                    data["service_lease"] = self._lease_payload(
                        leases.active_for_operation(operation_id)
                    )
                except Exception as lease_read_exc:
                    data["service_lease"] = None
                    data["service_recovery"]["lease_read_error_type"] = type(lease_read_exc).__name__
                result["allowed_actions"] = []
                result["retryable"] = False
        return result

    def _release_admin_request_lease(
        self,
        *,
        result: dict[str, Any],
        conn,
        leases: LeaseManager,
        operation_id: str,
        principal: ServicePrincipal,
        command: str,
    ) -> dict[str, Any]:
        """Release request-scoped admin ownership without reversing success."""
        try:
            active = leases.active_for_operation(operation_id)
            if active is not None and leases.is_owned_by(active, principal):
                leases.release(
                    operation_id,
                    principal,
                    reason=f"admin_command_complete:{command}",
                )
            result.setdefault("data", {})["service_lease"] = self._lease_payload(
                leases.active_for_operation(operation_id)
            )
            return result
        except Exception as exc:
            data = result.setdefault("data", {})
            fallback_applied = False
            fallback_error: Exception | None = None
            try:
                with immediate_transaction(conn, "admin_lease_cleanup_fallback"):
                    active = conn.execute(
                        "SELECT lease_id,owner_id,run_id FROM service_leases "
                        "WHERE operation_id=? AND released_at IS NULL",
                        (operation_id,),
                    ).fetchone()
                    if (
                        active is not None
                        and active["owner_id"] == principal.owner_id
                        and active["run_id"] == principal.run_id
                    ):
                        cursor = conn.execute(
                            "UPDATE service_leases SET released_at=?, release_reason=? "
                            "WHERE lease_id=? AND released_at IS NULL",
                            (
                                _now_stamp(),
                                f"admin cleanup fallback:{command}",
                                active["lease_id"],
                            ),
                        )
                        fallback_applied = cursor.rowcount == 1
                data["service_lease"] = self._lease_payload(
                    leases.active_for_operation(operation_id)
                )
                data["service_cleanup_warning"] = {
                    "kind": "admin_lease_release",
                    "operation_id": operation_id,
                    "command": command,
                    "error_type": type(exc).__name__,
                    "fallback_release_applied": fallback_applied,
                }
            except Exception as fallback_exc:
                fallback_error = fallback_exc
            if fallback_error is not None:
                data["service_recovery_required"] = True
                data["service_recovery"] = {
                    "kind": "admin_lease_release",
                    "operation_id": operation_id,
                    "command": command,
                    "error_type": type(exc).__name__,
                    "fallback_error_type": type(fallback_error).__name__,
                    "do_not_retry_command": True,
                }
                result["allowed_actions"] = []
                result["retryable"] = False
            return result

    def _reconcile_pending_start(
        self,
        *,
        conn,
        backend: Any,
        app: DishApplication,
        leases: LeaseManager,
        principal: ServicePrincipal,
        arguments: Mapping[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Return a proven start result after response loss, or fail uncertain."""
        task_gid = str(arguments.get("task_gid") or "").strip()
        kind = str(arguments.get("kind") or "").strip()
        if kind == "verification":
            from dish_tool.step7 import resolve_verification_start_target

            operation, cycle, _authority = resolve_verification_start_target(
                conn,
                task_gid=task_gid,
                target_operation_id=arguments.get("target_operation_id"),
                target_cycle_id=arguments.get("target_cycle_id"),
            )
        else:
            rows = conn.execute(
                """SELECT * FROM operations
                     WHERE task_gid=? AND status IN ('open','uncertain')
                     ORDER BY created_at DESC""",
                (task_gid,),
            ).fetchall()
            if len(rows) != 1 or rows[0]["status"] != "open":
                raise pending_error("start", request_id)
            operation = rows[0]
            cycle = None
        operation_id = operation["operation_id"]
        prepared_operation_id = str(arguments.get("prepared_operation_id") or "").strip()
        if prepared_operation_id and operation_id != prepared_operation_id:
            raise pending_error("start", request_id, operation_id=prepared_operation_id)

        if kind == "verification":
            data = replay_verification_read(
                conn, backend, operation_id=operation_id,
                agent=str(arguments.get("agent") or ""), run_id=principal.run_id,
                target_cycle_id=(
                    None if cycle is None else str(cycle["cycle_id"])
                ),
            )
        else:
            if (
                operation["operation_kind"] != kind
                or str(operation["run_id"] or "").strip() != principal.run_id
                or operation["phase"] != "prepare_required"
            ):
                raise pending_error("start", request_id, operation_id=operation_id)
            if kind == "change":
                intent = conn.execute(
                    """SELECT intended_json, completed_at FROM operation_steps
                         WHERE operation_id=? AND step_name='change_intent'""",
                    (operation_id,),
                ).fetchone()
                expected_intent = {
                    "level": arguments.get("change_level"),
                    "reason": arguments.get("change_reason"),
                }
                try:
                    recorded_intent = (
                        None if intent is None else json.loads(intent["intended_json"])
                    )
                except (TypeError, ValueError):
                    recorded_intent = None
                if (
                    intent is None
                    or not intent["completed_at"]
                    or recorded_intent != expected_intent
                ):
                    raise pending_error(
                        "start", request_id, operation_id=operation_id
                    )
            live = read_complete_task(
                backend, task_gid=task_gid, project_gid=COOKING_PROJECT_GID
            )
            if (
                live.identity != operation["expected_identity"]
                or live.section_gid != operation["expected_section_gid"]
            ):
                raise pending_error("start", request_id, operation_id=operation_id)
            role = "planning" if kind == "planning" else "research"
            release = self._release(role)
            registry = SectionRegistry.from_sections(
                backend.list_sections(COOKING_PROJECT_GID)
            )
            data = start_result_data(
                live=live, release=release, registry=registry, kind=kind,
                operation=operation, diagnostics=diagnostics_for(live, release),
            )

        active = leases.active_for_operation(operation_id)
        if active is None:
            cycle_id = (
                self._verification_lease_cycle_id(conn, operation_id, principal)
                if kind == "verification"
                else None
            )
            leases.acquire(
                operation_id, principal, context_cycle_id=cycle_id
            )
        elif not leases.is_owned_by(active, principal):
            raise pending_error("start", request_id, operation_id=operation_id)

        inspected = app.execute(
            "inspect",
            agent=str(arguments.get("agent") or "gpt"),
            submission_id=operation_id,
        )
        if not inspected.get("ok"):
            raise pending_error("start", request_id, operation_id=operation_id)
        data.update({
            "request_replayed": True,
            "request_id": request_id,
            "authoritative_view": inspected.get("data", {}).get("authoritative_view"),
        })
        replay_actions = list(inspected.get("allowed_actions", []))
        if kind == "verification" and "inspect" not in replay_actions:
            replay_actions.insert(0, "inspect")
        result = result_envelope(
            command="start",
            task_gid=task_gid,
            submission_id=operation_id,
            state=operation["status"],
            allowed_actions=replay_actions,
            data=data,
        )
        result = self._apply_principal_access(
            result, conn=conn, leases=leases, operation_id=operation_id,
            principal=principal,
            agent=str(arguments.get("agent") or "") or None,
        )
        if kind == "planning":
            with immediate_transaction(conn, "complete_planning_start_replay"):
                consume_planning_intent(
                    conn, request_id=request_id, operation_id=operation_id
                )
                complete_request(conn, request_id=request_id, result=result)
        else:
            complete_request(conn, request_id=request_id, result=result)
        return result


    def _begin_agent_execution(
        self,
        state: _AgentExecutionState,
        *,
        command: str,
        request_id: str | None,
    ) -> dict[str, Any] | None:
        prepared = state.prepared_arguments
        if command in _MUTATING_AGENT_COMMANDS and request_id:
            state.request_row, state.replay_started = begin_request(
                state.conn,
                request_id=request_id,
                owner_id=state.principal.owner_id,
                run_id=state.principal.run_id,
                command=command,
                arguments=prepared,
            )
            prior = stored_result(
                state.request_row,
                permit_uncertain_resume=command in {"approve", "reject", "submit"},
            )
            if prior is not None:
                return prior
            if (
                not state.replay_started
                and command != "start"
                and state.request_row["status"] != "uncertain"
            ):
                reconciled = self._reconcile_pending_operation_request(
                    conn=state.conn, command=command, request_id=request_id
                )
                if reconciled is not None:
                    return reconciled
        if command == "reject":
            prepared["reason"] = validate_rejection_reason(prepared.get("reason"))
        self._assert_connected_task_abandonment_access(
            state.conn, command=command, arguments=prepared
        )
        if command == "start" and prepared.get("kind") == "planning":
            if not request_id:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "Planning start requires a durable request ID",
                    rule="planning_intent_request_id_required",
                    retryable=False,
                    details={"field": "client.request_id"},
                )
            with immediate_transaction(state.conn, "planning_intent_gate"):
                confirmation = issue_or_claim_planning_intent(
                    state.conn,
                    request_id=request_id,
                    principal=state.principal,
                    arguments=prepared,
                )
                if confirmation is not None:
                    confirmation = complete_request(
                        state.conn, request_id=request_id, result=confirmation
                    )
            if confirmation is not None:
                return confirmation
        return None

    def _build_agent_application(
        self,
        state: _AgentExecutionState,
        *,
        command: str,
        request_id: str | None,
    ) -> None:
        state.backend = self.backend_factory()
        if command not in _READ_ONLY_AGENT_COMMANDS:
            self._assert_mutation_ready(state.backend)
        state.app = DishApplication(
            state.conn,
            state.backend,
            release_loader=lambda role=None: self._release(role),
            invocation_run_id=state.invocation_run_id,
            invocation_request_id=request_id,
        )

    def _resolve_agent_operation(
        self,
        state: _AgentExecutionState,
        *,
        command: str,
    ) -> None:
        prepared = state.prepared_arguments
        state.operation_id = self._operation_for_request(
            state.conn, command, prepared
        )
        if command == "start" and prepared.get("prepared_operation_id"):
            authority = state.conn.execute(
                """SELECT abandonment.abandoned_owner_id, abandonment.abandoned_run_id
                     FROM operation_successions AS succession
                     JOIN abandonment_attempts AS abandonment
                       ON abandonment.abandonment_id=succession.abandonment_id
                    WHERE succession.successor_operation_id=?""",
                (state.operation_id,),
            ).fetchone()
            if (
                authority is not None
                and authority["abandoned_owner_id"] == state.principal.owner_id
                and authority["abandoned_run_id"] == state.principal.run_id
            ):
                raise DishRuleError(
                    "AGENT_MISMATCH",
                    "the abandoned client run cannot claim its replacement attempt",
                    rule="abandoned_run_claim_forbidden",
                )
        if command == "start" and prepared.get("kind") == "verification":
            from dish_tool.step7 import resolve_verification_start_target

            target_operation, target_cycle, authority = resolve_verification_start_target(
                state.conn,
                task_gid=str(prepared.get("task_gid") or "").strip(),
                target_operation_id=prepared.get("target_operation_id"),
                target_cycle_id=prepared.get("target_cycle_id"),
            )
            state.operation_id = str(target_operation["operation_id"])
            state.verification_start_cycle_id = str(target_cycle["cycle_id"])
            if (
                authority is not None
                and authority["abandoned_owner_id"] == state.principal.owner_id
                and authority["abandoned_run_id"] == state.principal.run_id
            ):
                raise DishRuleError(
                    "AGENT_MISMATCH",
                    "the abandoned client run cannot claim its replacement Verification attempt",
                    rule="abandoned_run_claim_forbidden",
                )
        if state.operation_id and command in _MUTATING_AGENT_COMMANDS:
            self._assert_connected_abandonment_access(
                state.conn,
                command=command,
                arguments=prepared,
                operation_id=state.operation_id,
            )

    def _acquire_agent_lease(
        self,
        state: _AgentExecutionState,
        *,
        command: str,
    ) -> None:
        prepared = state.prepared_arguments
        operation_id = state.operation_id
        if command in _LEASED_AGENT_COMMANDS:
            if not operation_id:
                raise DishRuleError(
                    "NOT_FOUND", "operation not found", rule="operation_not_found"
                )
            operation = self._operation_row(state.conn, operation_id)
            if operation is None:
                raise DishRuleError(
                    "NOT_FOUND", "operation not found", rule="operation_not_found"
                )
            state.completed_submit = bool(
                command == "submit"
                and operation["status"] == "completed"
                and operation["terminal_outcome"] == "destination_handled"
            )
            if operation["status"] != "open" and not state.completed_submit:
                raise DishRuleError(
                    "WRONG_STATE",
                    "operation is not open",
                    rule="operation_not_open",
                    details={"actual": operation["status"]},
                )
            active = state.leases.active_for_operation(operation_id)
            if state.completed_submit:
                return
            if active is None:
                if not self._may_claim_missing_lease(
                    state.conn, operation_id, state.principal, command
                ):
                    raise DishRuleError(
                        "AGENT_MISMATCH",
                        "operation has no lease and this run has no durable workflow ownership",
                        rule="service_lease_claim_forbidden",
                        details={
                            "operation_id": operation_id,
                            "run_id": state.principal.run_id,
                        },
                    )
                cycle_id = self._reclaimed_lease_cycle_id(
                    state.conn, operation_id, state.principal, command
                )
                state.leases.acquire(
                    operation_id, state.principal, context_cycle_id=cycle_id
                )
                state.acquired_for_request = True
            else:
                state.leases.assert_owned(operation_id, state.principal)
            return
        if command == "start" and prepared.get("kind") == "verification":
            if not operation_id:
                raise DishRuleError(
                    "NOT_FOUND",
                    "task has no open operation",
                    rule="open_operation_missing",
                )
            active = state.leases.active_for_operation(operation_id)
            if active is None:
                state.leases.acquire(
                    operation_id,
                    state.principal,
                    context_cycle_id=(
                        state.verification_start_cycle_id
                        or self._verification_lease_cycle_id(
                            state.conn,
                            operation_id,
                            state.principal,
                            pending_first=True,
                        )
                    ),
                )
                state.acquired_for_request = True
            else:
                state.leases.assert_owned(operation_id, state.principal)

    def _dispatch_agent_command(
        self,
        state: _AgentExecutionState,
        *,
        command: str,
    ) -> dict[str, Any]:
        if state.completed_submit:
            from dish_tool.step9 import completed_submit_live

            operation = self._operation_row(state.conn, str(state.operation_id))
            release = self._release("verification")
            data = completed_submit_live(
                state.conn,
                state.backend,
                operation_id=state.operation_id,
                schema=release.schema,
            )
            view = state.app.operation_service.authoritative_view(
                state.operation_id, schema=release.schema
            )
            return result_envelope(
                command="submit",
                task_gid=operation["task_gid"],
                submission_id=state.operation_id,
                state=view["status"],
                allowed_actions=view["legal_actions"],
                data={**data, "authoritative_view": view},
                validation_scope=scope_for_command("submit"),
            )
        with self._candidate_file(state.prepared_arguments) as prepared:
            if command == "start" and prepared.get("kind") == "planning":
                prepared = dict(prepared)
                prepared.pop("intent_challenge_id", None)
                prepared.pop("intent_basis", None)
                prepared.pop("override_reason", None)
            return state.app.execute(command, **prepared)

    def _finish_agent_result(
        self,
        state: _AgentExecutionState,
        *,
        command: str,
        request_id: str | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        result = _preserve_semantic_evidence_result(
            result,
            execution_occurred=True,
            request_id_consumed=bool(request_id and state.replay_started),
        )
        prepared = state.prepared_arguments
        if (
            command == "start"
            and prepared.get("kind") != "verification"
            and result.get("ok")
        ):
            state.operation_id = result.get("submission_id")
            if state.operation_id:
                try:
                    state.leases.acquire(state.operation_id, state.principal)
                    state.acquired_for_request = True
                except Exception as exc:
                    data = result.setdefault("data", {})
                    data["service_recovery_required"] = True
                    data["service_recovery"] = {
                        "kind": "lease_acquisition",
                        "operation_id": state.operation_id,
                        "error_type": type(exc).__name__,
                        "do_not_retry_command": True,
                    }
                    result["allowed_actions"] = []
                    result["retryable"] = False
        result_operation_id = state.operation_id or result.get("submission_id")
        if result.get("ok") and result_operation_id and command in _MUTATING_AGENT_COMMANDS:
            result = self._finalize_successful_lease(
                result=result,
                conn=state.conn,
                leases=state.leases,
                operation_id=result_operation_id,
                principal=state.principal,
                command=command,
            )
        elif not result.get("ok") and state.acquired_for_request and state.operation_id:
            if command == "start" and prepared.get("kind") == "verification":
                state.leases.release(
                    state.operation_id,
                    state.principal,
                    reason="verification_start_failed",
                )
            elif command in _LEASED_AGENT_COMMANDS:
                state.leases.release(
                    state.operation_id,
                    state.principal,
                    reason="reclaimed_command_rejected",
                )
        result = self._apply_principal_access(
            result,
            conn=state.conn,
            leases=state.leases,
            operation_id=result_operation_id,
            principal=state.principal,
            agent=str(prepared.get("agent") or "") or None,
        )
        if result.get("data", {}).get("service_recovery_required"):
            result["allowed_actions"] = []
        if request_id and command in _MUTATING_AGENT_COMMANDS:
            result.setdefault("data", {})["request_id"] = request_id
            if (
                command == "start"
                and prepared.get("kind") == "planning"
                and result.get("ok")
                and result_operation_id
            ):
                with immediate_transaction(
                    state.conn, "complete_planning_start_request"
                ):
                    consume_planning_intent(
                        state.conn,
                        request_id=request_id,
                        operation_id=str(result_operation_id),
                    )
                    complete_request(
                        state.conn, request_id=request_id, result=result
                    )
            else:
                complete_request(state.conn, request_id=request_id, result=result)
        return result

    def _release_rejected_request_lease(
        self,
        *,
        leases: LeaseManager,
        operation_id: str,
        principal: ServicePrincipal,
        command: str,
        reason: str,
    ) -> dict[str, Any] | None:
        """Preserve the rule error while exposing failed lease cleanup."""

        try:
            leases.release(operation_id, principal, reason=reason)
            return None
        except Exception as exc:  # explicit cleanup boundary; original rule stays primary
            warning: dict[str, Any] = {
                "kind": "rejected_command_lease_release",
                "operation_id": operation_id,
                "command": command,
                "error_type": type(exc).__name__,
                "do_not_retry_command": True,
            }
            try:
                active = leases.active_for_operation(operation_id)
                warning["lease_still_active"] = active is not None
                if active is not None:
                    warning["lease_id"] = active["lease_id"]
            except Exception as read_exc:
                warning["lease_state_unknown"] = True
                warning["lease_read_error_type"] = type(read_exc).__name__
            return warning

    def _agent_rule_error_result(
        self,
        state: _AgentExecutionState,
        *,
        command: str,
        arguments: Mapping[str, Any],
        request_id: str | None,
        error: DishRuleError,
    ) -> dict[str, Any]:
        error = _preserve_semantic_evidence_error(
            error,
            execution_occurred=True,
            request_id_consumed=bool(request_id and state.replay_started),
        )
        cleanup_warning = None
        if state.acquired_for_request and state.operation_id:
            cleanup_warning = self._release_rejected_request_lease(
                leases=state.leases,
                operation_id=state.operation_id,
                principal=state.principal,
                command=command,
                reason="service_command_rejected",
            )
        operation_id = state.operation_id or (
            str(arguments.get("submission_id") or "").strip() or None
        )
        operation_kind = None
        task_gid = None
        if operation_id:
            row = state.conn.execute(
                "SELECT operation_kind, task_gid FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                operation_kind = row["operation_kind"]
                task_gid = row["task_gid"]
        validation_scope = (
            scope_for_command(command, operation_kind=operation_kind)
            if operation_id
            else ()
        )
        result = error_envelope(
            command,
            error,
            task_gid=task_gid,
            submission_id=operation_id,
            validation_scope=validation_scope,
        )
        if cleanup_warning is not None:
            data = result.setdefault("data", {})
            data["service_cleanup_warning"] = cleanup_warning
            data["service_recovery_required"] = True
            self._synchronize_exposed_actions(result, [])
            result["retryable"] = False
        if error.rule == "service_lease_expired":
            view = (
                self._exposed_operation_view(
                    state.conn, operation_id, app=state.app
                )
                if operation_id
                else None
            )
            result = self._apply_expired_lease_guidance(
                result,
                operation_id=operation_id or "unknown",
                after_recovery_actions=(
                    list(view.get("legal_actions") or []) if view is not None else []
                ),
                authoritative_view=view,
            )
        if request_id and command in _MUTATING_AGENT_COMMANDS and state.replay_started:
            result.setdefault("data", {})["request_id"] = request_id
            complete_request(state.conn, request_id=request_id, result=result)
        return result

    def execute_agent(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._agent_requests.execute(
            command, arguments, principal=principal, request_id=request_id
        )


    def record_replay_validation_failure(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str,
        error: DishRuleError,
    ) -> dict[str, Any]:
        """Persist pre-application validation outcomes for replay-sensitive calls."""
        with self._maintenance_gate.request():
            conn = self._initialize_database(
                surface="agent-validation",
                command=command,
                request_id=request_id,
                principal=principal,
                task_gid=str(arguments.get("task_gid") or "").strip() or None,
                operation_id=(
                    str(
                        arguments.get("submission_id")
                        or arguments.get("operation_id")
                        or ""
                    ).strip()
                    or None
                ),
            )
            try:
                row, started = begin_request(
                    conn,
                    request_id=request_id,
                    owner_id=principal.owner_id,
                    run_id=principal.run_id,
                    command=command,
                    arguments=arguments,
                )
                prior = stored_result(row)
                if prior is not None:
                    return prior
                if not started:
                    raise pending_error(command, request_id)
                result = error_envelope(command, error)
                result.setdefault("data", {})["request_id"] = request_id
                complete_request(conn, request_id=request_id, result=result)
                return result
            finally:
                conn.close()

    def renew_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._shadow_capture.execute(
            command="renew-lease",
            arguments={"operation_id": operation_id},
            principal=principal,
            request_id=request_id,
            call=lambda: self._renew_lease(
                operation_id, principal, request_id=request_id
            ),
        )

    def _renew_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            try:
                conn = self._initialize_database(
                    surface="lease",
                    command="renew-lease",
                    request_id=request_id,
                    principal=principal,
                    operation_id=operation_id,
                )
            except Exception as exc:
                return error_envelope(
                    "renew-lease",
                    _database_initialization_error(exc),
                    submission_id=operation_id,
                )
            replay_started = False
            try:
                if request_id:
                    row, replay_started = begin_request(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command="renew-lease",
                        arguments={"operation_id": operation_id},
                    )
                    prior = stored_result(row)
                    if prior is not None:
                        return prior
                    if not replay_started:
                        raise pending_error("renew-lease", request_id, operation_id=operation_id)
                operation = conn.execute(
                    "SELECT task_gid, status FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if operation is not None and operation["status"] != "open":
                    raise DishRuleError(
                        "WRONG_STATE",
                        "operation is not open",
                        rule="operation_not_open",
                        details={"actual": operation["status"]},
                    )
                leases = self._lease_manager(conn)
                transaction = (
                    immediate_transaction(conn, "renew_service_lease_request")
                    if request_id
                    else contextlib.nullcontext()
                )
                with transaction:
                    row = leases.renew(
                        operation_id, principal,
                        manage_transaction=not bool(request_id),
                    )
                    result = result_envelope(
                        command="renew-lease",
                        task_gid=None if operation is None else operation["task_gid"],
                        submission_id=operation_id,
                        state=None if operation is None else operation["status"],
                        data={"service_lease": self._lease_payload(row)},
                    )
                    if request_id:
                        result.setdefault("data", {})["request_id"] = request_id
                        complete_request(conn, request_id=request_id, result=result)
                    return result
            except DishRuleError as exc:
                exc = _preserve_semantic_evidence_error(
                    exc,
                    execution_occurred=True,
                    request_id_consumed=bool(request_id and replay_started),
                )
                operation = conn.execute(
                    "SELECT task_gid, status FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                result = error_envelope(
                    "renew-lease",
                    exc,
                    task_gid=None if operation is None else operation["task_gid"],
                    submission_id=operation_id,
                    state=None if operation is None else operation["status"],
                )
                if exc.rule == "service_lease_expired":
                    view = self._exposed_operation_view(conn, operation_id)
                    result = self._apply_expired_lease_guidance(
                        result,
                        operation_id=operation_id,
                        after_recovery_actions=(
                            list(view.get("legal_actions") or [])
                            if view is not None
                            else []
                        ),
                        authoritative_view=view,
                    )
                if request_id and replay_started:
                    result.setdefault("data", {})["request_id"] = request_id
                    complete_request(conn, request_id=request_id, result=result)
                return result
            finally:
                conn.close()

    def recover_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._shadow_capture.execute(
            command="recover-lease",
            arguments={"operation_id": operation_id, "reason": reason},
            principal=principal,
            request_id=request_id,
            call=lambda: self._recover_lease(
                operation_id, principal, reason=reason, request_id=request_id
            ),
        )

    def _recover_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            try:
                conn = self._initialize_database(
                    surface="lease",
                    command="recover-lease",
                    request_id=request_id,
                    principal=principal,
                    operation_id=operation_id,
                )
            except Exception as exc:
                return error_envelope(
                    "recover-lease",
                    _database_initialization_error(exc),
                    submission_id=operation_id,
                )
            replay_started = False
            try:
                if request_id:
                    row, replay_started = begin_request(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command="recover-lease",
                        arguments={"operation_id": operation_id, "reason": reason},
                    )
                    prior = stored_result(row)
                    if prior is not None:
                        return prior
                    if not replay_started:
                        raise pending_error("recover-lease", request_id)
                operation = conn.execute(
                    "SELECT task_gid, status FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if operation is None:
                    raise DishRuleError(
                        "NOT_FOUND",
                        "operation not found",
                        rule="operation_not_found",
                    )
                if operation["status"] != "open":
                    raise DishRuleError(
                        "WRONG_STATE",
                        "operation is not open",
                        rule="operation_not_open",
                        details={"actual": operation["status"]},
                    )
                leases = self._lease_manager(conn)
                transaction = (
                    immediate_transaction(conn, "recover_service_lease_request")
                    if request_id
                    else contextlib.nullcontext()
                )
                with transaction:
                    released = leases.admin_recover(
                        operation_id, principal, reason=reason,
                        manage_transaction=not bool(request_id),
                    )
                    if released is None:
                        raise DishRuleError(
                            "CONFLICT",
                            "operation has no active service lease",
                            rule="service_lease_missing",
                        )
                    result = result_envelope(
                        command="recover-lease",
                        task_gid=operation["task_gid"],
                        submission_id=operation_id,
                        state=operation["status"],
                        data={
                            "service_lease": None,
                            "released_lease_id": released["lease_id"],
                            "ownership_transferred": False,
                        },
                    )
                    if request_id:
                        result.setdefault("data", {})["request_id"] = request_id
                        complete_request(conn, request_id=request_id, result=result)
                    return result
            except DishRuleError as exc:
                exc = _preserve_semantic_evidence_error(
                    exc,
                    execution_occurred=True,
                    request_id_consumed=bool(request_id and replay_started),
                )
                row = conn.execute(
                    "SELECT task_gid FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                result = error_envelope(
                    "recover-lease",
                    exc,
                    task_gid=None if row is None else row["task_gid"],
                    submission_id=operation_id,
                )
                if request_id and replay_started:
                    result.setdefault("data", {})["request_id"] = request_id
                    complete_request(conn, request_id=request_id, result=result)
                return result
            finally:
                conn.close()

    def expire_lease(
        self,
        principal: ServicePrincipal,
        *,
        lease_id: str | None = None,
        task_gid: str | None = None,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        arguments = {
            **({"lease_id": lease_id} if lease_id is not None else {}),
            **({"task_gid": task_gid} if task_gid is not None else {}),
            "reason": reason,
        }
        return self._shadow_capture.execute(
            command="expire-lease",
            arguments=arguments,
            principal=principal,
            request_id=request_id,
            call=lambda: self._expire_lease(
                principal, lease_id=lease_id, task_gid=task_gid, reason=reason,
                request_id=request_id,
            ),
        )

    def _expire_lease(
        self,
        principal: ServicePrincipal,
        *,
        lease_id: str | None = None,
        task_gid: str | None = None,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Release one exact or task-selected lease without changing workflow state."""

        command = "expire-lease"
        if (lease_id is None) == (task_gid is None):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "exactly one of lease_id and task_gid is required",
                rule="lease_expiry_target_invalid",
                details={"fields": ["lease_id", "task_gid"]},
            )
        request_id = require_dish_uuid(request_id, field="client.request_id")
        if lease_id is not None:
            lease_id = require_dish_uuid(lease_id, field="lease_id")
        if task_gid is not None:
            task_gid = require_asana_gid(task_gid, field="task_gid")
        if not isinstance(reason, str):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "reason must be a JSON string",
                rule="lease_expiry_reason_invalid",
                details={"field": "reason"},
            )
        reason = reason.strip()
        if not reason:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "lease expiry reason is required",
                rule="lease_expiry_reason_required",
                details={"field": "reason"},
            )
        arguments = {
            **({"lease_id": lease_id} if lease_id is not None else {}),
            **({"task_gid": task_gid} if task_gid is not None else {}),
            "reason": reason,
        }
        with self._maintenance_gate.request():
            try:
                conn = self._initialize_database(
                    surface="admin-lease-expiry",
                    command=command,
                    request_id=request_id,
                    principal=principal,
                    task_gid=task_gid,
                )
            except Exception as exc:
                result = error_envelope(
                    command,
                    _database_initialization_error(exc),
                    task_gid=task_gid,
                )
                result["allowed_actions"] = []
                result.setdefault("data", {})["request_id"] = request_id
                return result

            try:
                try:
                    request_row, _started = begin_request(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command=command,
                        arguments=arguments,
                    )
                except DishRuleError as exc:
                    result = error_envelope(command, exc, task_gid=task_gid)
                    result["allowed_actions"] = []
                    result.setdefault("data", {})["request_id"] = request_id
                    return result

                prior = stored_result(request_row)
                if prior is not None:
                    prior["allowed_actions"] = []
                    return prior

                with immediate_transaction(conn, "expire_service_lease_request"):
                    current_request = conn.execute(
                        "SELECT * FROM service_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    prior = stored_result(current_request)
                    if prior is not None:
                        prior["allowed_actions"] = []
                        return prior

                    leases = self._lease_manager(conn)
                    selected = (
                        leases.by_id(lease_id)
                        if lease_id is not None
                        else leases.active_for_task(str(task_gid))
                    )

                    operation = None
                    if selected is not None:
                        operation = conn.execute(
                            "SELECT task_gid,status FROM operations WHERE operation_id=?",
                            (selected["operation_id"],),
                        ).fetchone()
                        if operation is None:
                            raise DishRuleError(
                                "INTERNAL_ERROR",
                                "service lease lost its operation",
                                rule="service_lease_operation_missing",
                                details={"lease_id": selected["lease_id"]},
                            )

                    if selected is None and lease_id is not None:
                        error = DishRuleError(
                            "NOT_FOUND",
                            "service lease not found",
                            rule="service_lease_not_found",
                            details={"lease_id": lease_id},
                        )
                        result = error_envelope(command, error)
                    elif selected is None:
                        result = result_envelope(
                            command=command,
                            task_gid=task_gid,
                            allowed_actions=[],
                            data={
                                "outcome": "no_active_lease",
                                "released": False,
                                "lease": None,
                                "ownership_transferred": False,
                                "request_id": request_id,
                            },
                        )
                    elif selected["released_at"] is not None:
                        result = result_envelope(
                            command=command,
                            task_gid=selected["task_gid"],
                            submission_id=selected["operation_id"],
                            state=operation["status"],
                            allowed_actions=[],
                            data={
                                "outcome": "already_released",
                                "released": False,
                                "lease": self._lease_expiry_payload(selected),
                                "ownership_transferred": False,
                                "request_id": request_id,
                            },
                        )
                    else:
                        claim = live_operation_execution_claim(
                            conn, operation_id=selected["operation_id"]
                        )
                        if claim is not None:
                            error = DishRuleError(
                                "CONFLICT",
                                "another mutation is already executing for this operation",
                                rule="operation_mutation_in_progress",
                                retryable=True,
                                details={
                                    "operation_id": selected["operation_id"],
                                    "command": claim["command"],
                                    "acquired_at": claim["acquired_at"],
                                },
                            )
                            result = error_envelope(
                                command,
                                error,
                                task_gid=selected["task_gid"],
                                submission_id=selected["operation_id"],
                                state=operation["status"],
                            )
                        else:
                            released, changed = leases.admin_expire_selected(
                                selected["lease_id"],
                                reason=f"admin expiry: {reason}",
                                manage_transaction=False,
                            )
                            result = result_envelope(
                                command=command,
                                task_gid=released["task_gid"],
                                submission_id=released["operation_id"],
                                state=operation["status"],
                                allowed_actions=[],
                                data={
                                    "outcome": (
                                        "released" if changed else "already_released"
                                    ),
                                    "released": changed,
                                    "lease": self._lease_expiry_payload(released),
                                    "ownership_transferred": False,
                                    "request_id": request_id,
                                },
                            )

                    result["allowed_actions"] = []
                    result.setdefault("data", {})["request_id"] = request_id
                    complete_request(conn, request_id=request_id, result=result)
                    return result
            finally:
                conn.close()

    def record_agent_argument_failure(
        self,
        command: str,
        error_payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            conn = self._initialize_database(
                surface="agent-argument-validation",
                command=command,
                task_gid=str(context.get("task_gid") or "").strip() or None,
                operation_id=(
                    str(context.get("submission_id") or "").strip() or None
                ),
            )
            backend = self.backend_factory()
            try:
                app = DishApplication(
                    conn,
                    backend,
                    release_loader=lambda role=None: self._release(role),
                )
                error = DishRuleError(
                    str(error_payload.get("code") or "INVALID_ARGUMENT"),
                    str(error_payload.get("message") or "invalid arguments"),
                    rule=error_payload.get("rule"),
                    retryable=bool(error_payload.get("retryable", False)),
                    details=error_payload.get("details") if isinstance(error_payload.get("details"), dict) else None,
                    errors=error_payload.get("errors") if isinstance(error_payload.get("errors"), list) else None,
                )
                return app.record_argument_failure(
                    command,
                    error,
                    agent=context.get("agent"),
                    task_gid=context.get("task_gid"),
                    submission_id=context.get("submission_id"),
                )
            finally:
                self._close_backend(backend)
                conn.close()

    def record_admin_argument_failure(
        self,
        command: str,
        error_payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            conn = self._initialize_database(
                surface="admin-argument-validation",
                command=command,
                operation_id=(
                    str(context.get("submission_id") or "").strip() or None
                ),
            )
            backend = self.backend_factory()
            try:
                app = DishAdminApplication(
                    conn,
                    backend=backend,
                    release_loader=lambda: self._release(None, include_migrations=True),
                )
                error = DishRuleError(
                    str(error_payload.get("code") or "INVALID_ARGUMENT"),
                    str(error_payload.get("message") or "invalid arguments"),
                    rule=error_payload.get("rule"),
                    retryable=bool(error_payload.get("retryable", False)),
                    details=error_payload.get("details") if isinstance(error_payload.get("details"), dict) else None,
                    errors=error_payload.get("errors") if isinstance(error_payload.get("errors"), list) else None,
                )
                return app.record_argument_failure(
                    command,
                    error,
                    submission_id=context.get("submission_id"),
                )
            finally:
                self._close_backend(backend)
                conn.close()


    def _prepare_admin_execution_state(
        self,
        conn: sqlite3.Connection,
        *,
        command: str,
        arguments: Mapping[str, Any],
        principal: ServicePrincipal,
        requested_operation_id: str | None,
    ) -> _AdminExecutionState:
        prepared = dict(arguments)
        if command == "reconcile-abandonment":
            raw_abandonment_id = str(prepared.get("abandonment_id") or "").strip()
            if raw_abandonment_id:
                prepared["abandonment_id"] = resolve_admin_abandonment_target(
                    conn, raw_abandonment_id
                )
            abandonment_id = str(prepared.get("abandonment_id") or "").strip()
            if abandonment_id:
                abandonment = conn.execute(
                    "SELECT source_operation_id FROM abandonment_attempts WHERE abandonment_id=?",
                    (abandonment_id,),
                ).fetchone()
                if abandonment is not None:
                    requested_operation_id = str(abandonment["source_operation_id"])
        if command in _ADMIN_OPERATION_TARGET_COMMANDS:
            raw_submission_id = str(prepared.get("submission_id") or "").strip()
            if raw_submission_id:
                prepared["submission_id"] = resolve_admin_operation_target(
                    conn, raw_submission_id
                )
        supplied_run_id = str(prepared.get("run_id") or "").strip()
        if command in _RUN_ID_ADMIN_COMMANDS and not supplied_run_id:
            prepared["run_id"] = principal.run_id
        operation_id = (
            str(prepared.get("submission_id") or "").strip()
            or requested_operation_id
            or None
        )
        return _AdminExecutionState(
            conn=conn,
            principal=principal,
            leases=self._lease_manager(conn),
            prepared_arguments=prepared,
            operation_id=operation_id,
            supplied_run_id=supplied_run_id,
        )

    def _begin_admin_execution(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
        request_id: str | None,
    ) -> dict[str, Any] | None:
        if request_id:
            request_row, state.replay_started = begin_request(
                state.conn,
                request_id=request_id,
                owner_id=state.principal.owner_id,
                run_id=state.principal.run_id,
                command=command,
                arguments=state.prepared_arguments,
            )
            prior = stored_result(
                request_row,
                permit_uncertain_resume=command
                in {
                    "repair-destination",
                    "discard",
                    "abandon-operation",
                    "reconcile-abandonment",
                },
            )
            if prior is not None:
                return prior
            if (
                not state.replay_started
                and request_row["status"] != "uncertain"
                and command != "reopen-planning"
            ):
                reconciled = self._reconcile_pending_operation_request(
                    conn=state.conn, command=command, request_id=request_id
                )
                if reconciled is not None:
                    return reconciled
        if (
            command in _RUN_ID_ADMIN_COMMANDS
            and state.supplied_run_id
            and state.supplied_run_id != state.principal.run_id
        ):
            raise DishRuleError(
                "AGENT_MISMATCH",
                "command run identity conflicts with the authenticated admin run",
                rule="service_run_id_conflict",
                details={
                    "client_run_id": state.principal.run_id,
                    "command_run_id": state.supplied_run_id,
                },
            )
        self._capture_exact_admin_recovery_authority(state, command=command)
        return self._validate_admin_execution_arguments(
            state, command=command, request_id=request_id
        )

    def _capture_exact_admin_recovery_authority(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
    ) -> None:
        if (
            command not in _OPERATION_ADMIN_COMMANDS
            or command in _LEASE_FREE_ADMIN_COMMANDS
            or not state.operation_id
        ):
            return
        existing = state.leases.active_for_operation(state.operation_id)
        if existing is None or state.leases.is_owned_by(existing, state.principal):
            return
        recovery = _assert_existing_admin_lease_access(
            state.conn,
            state.leases,
            command=command,
            operation_id=state.operation_id,
            principal=state.principal,
            existing=existing,
        )
        if recovery is not None:
            state.exact_recovery_execution_id = str(recovery["execution_id"])
            state.exact_recovery_lease_id = str(existing["lease_id"])

    def _validate_admin_execution_arguments(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
        request_id: str | None,
    ) -> dict[str, Any] | None:
        app = DishAdminApplication(
            state.conn,
            invocation_request_id=request_id,
            invocation_run_id=state.principal.run_id,
        )
        try:
            app.validate_arguments(command, state.prepared_arguments)
        except DishRuleError as exc:
            result = app.record_argument_failure(
                command, exc, submission_id=state.operation_id
            )
            if request_id:
                result.setdefault("data", {})["request_id"] = request_id
                complete_request(state.conn, request_id=request_id, result=result)
            return result
        if request_id and not state.replay_started and command == "reopen-planning":
            return self._complete_terminal_planning_reopen_request(
                conn=state.conn, request_id=request_id
            )
        return None

    def _build_admin_backend(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
        request_id: str | None,
    ) -> dict[str, Any] | None:
        state.backend = self.backend_factory()
        self._assert_mutation_ready(state.backend)
        if request_id and not state.replay_started and command == "reopen-planning":
            return self._reconcile_pending_planning_reopen_request(
                conn=state.conn, backend=state.backend, request_id=request_id
            )
        return None

    def _acquire_admin_execution_lease(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
    ) -> None:
        if (
            command not in _OPERATION_ADMIN_COMMANDS
            or command in _LEASE_FREE_ADMIN_COMMANDS
            or not state.operation_id
        ):
            return
        existing = state.leases.active_for_operation(state.operation_id)
        if existing is None:
            state.leases.acquire(
                state.operation_id, state.principal, lease_kind="admin_request"
            )
            state.acquired_for_request = True
            return
        recovery = _assert_existing_admin_lease_access(
            state.conn,
            state.leases,
            command=command,
            operation_id=state.operation_id,
            principal=state.principal,
            existing=existing,
        )
        if recovery is None:
            return
        current_execution_id = str(recovery["execution_id"])
        current_lease_id = str(existing["lease_id"])
        execution_changed = (
            state.exact_recovery_execution_id is not None
            and state.exact_recovery_execution_id != current_execution_id
        )
        lease_changed = (
            state.exact_recovery_lease_id is not None
            and state.exact_recovery_lease_id != current_lease_id
        )
        if execution_changed or lease_changed:
            raise DishRuleError(
                "CONFLICT",
                "exact recovery lease authority changed before execution",
                rule="service_lease_conflict",
                details={"operation_id": state.operation_id},
            )
        state.exact_recovery_execution_id = current_execution_id
        state.exact_recovery_lease_id = current_lease_id

    def _dispatch_admin_command(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        app = DishAdminApplication(
            state.conn,
            backend=state.backend,
            release_loader=lambda: self._release(None, include_migrations=True),
            invocation_request_id=request_id,
            invocation_run_id=state.principal.run_id,
        )
        with self._candidate_file(state.prepared_arguments) as prepared:
            return app.execute(command, **prepared)

    def _complete_resumed_admin_request(
        self,
        state: _AdminExecutionState,
        *,
        request_id: str | None,
        result: dict[str, Any],
    ) -> None:
        data = result.get("data")
        resumed = data.pop("resumed_admin_execution", None) if isinstance(data, dict) else None
        if (
            not result.get("ok")
            or not isinstance(resumed, dict)
            or not resumed.get("request_id")
            or resumed.get("request_id") == request_id
        ):
            return
        original_result = json.loads(json.dumps(result))
        original_result["command"] = str(resumed["command"])
        complete_request(
            state.conn,
            request_id=str(resumed["request_id"]),
            result=original_result,
        )

    def _finish_admin_result(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
        request_id: str | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self._complete_resumed_admin_request(
            state, request_id=request_id, result=result
        )
        result = _preserve_semantic_evidence_result(
            result,
            execution_occurred=True,
            request_id_consumed=bool(request_id and state.replay_started),
        )
        if result.get("ok") and state.operation_id:
            result = self._finalize_successful_lease(
                result=result,
                conn=state.conn,
                leases=state.leases,
                operation_id=state.operation_id,
                principal=state.principal,
                command=command,
                admin=True,
                exact_recovery_execution_id=state.exact_recovery_execution_id,
                exact_recovery_lease_id=state.exact_recovery_lease_id,
            )
            result = self._release_admin_request_lease(
                result=result,
                conn=state.conn,
                leases=state.leases,
                operation_id=state.operation_id,
                principal=state.principal,
                command=command,
            )
        elif not result.get("ok") and state.acquired_for_request and state.operation_id:
            state.leases.release(
                state.operation_id,
                state.principal,
                reason="admin_command_rejected",
            )
        if request_id:
            result.setdefault("data", {})["request_id"] = request_id
            unresolved_reopen = (
                command == "reopen-planning"
                and result.get("code") == "BACKEND_UNCERTAIN"
                and planning_reopen_attempt_by_request(
                    state.conn, request_id=request_id
                )
                is not None
            )
            if not unresolved_reopen:
                complete_request(state.conn, request_id=request_id, result=result)
        return result

    def _admin_rule_error_result(
        self,
        state: _AdminExecutionState,
        *,
        command: str,
        request_id: str | None,
        error: DishRuleError,
    ) -> dict[str, Any]:
        error = _preserve_semantic_evidence_error(
            error,
            execution_occurred=True,
            request_id_consumed=bool(request_id and state.replay_started),
        )
        cleanup_warning = None
        if state.acquired_for_request and state.operation_id:
            cleanup_warning = self._release_rejected_request_lease(
                leases=state.leases,
                operation_id=state.operation_id,
                principal=state.principal,
                command=command,
                reason="admin_command_rejected",
            )
        task_gid = None
        if state.operation_id:
            row = state.conn.execute(
                "SELECT task_gid FROM operations WHERE operation_id=?",
                (state.operation_id,),
            ).fetchone()
            task_gid = None if row is None else row["task_gid"]
        result = error_envelope(
            command,
            error,
            task_gid=task_gid,
            submission_id=state.operation_id,
        )
        if cleanup_warning is not None:
            data = result.setdefault("data", {})
            data["service_cleanup_warning"] = cleanup_warning
            data["service_recovery_required"] = True
            self._synchronize_exposed_actions(result, [])
            result["retryable"] = False
        if request_id and state.replay_started:
            result.setdefault("data", {})["request_id"] = request_id
            complete_request(state.conn, request_id=request_id, result=result)
        return result

    def execute_admin(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._admin_requests.execute(
            command, arguments, principal=principal, request_id=request_id
        )


    @staticmethod
    def _backup_result_matches_record(
        result: Mapping[str, Any], record: BackupRecord
    ) -> bool:
        data = result.get("data")
        backup = data.get("backup") if isinstance(data, Mapping) else None
        return bool(
            result.get("ok")
            and isinstance(backup, Mapping)
            and backup.get("backup_id") == record.backup_id
            and backup.get("sha256") == record.sha256
            and backup.get("size_bytes") == record.size_bytes
        )

    def _commit_backup_creation_result(
        self,
        conn: sqlite3.Connection,
        *,
        request_id: str,
        record: BackupRecord,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit backup metadata and the replay result as one SQLite unit."""

        class _AuthoritativeReplayWon(Exception):
            def __init__(self, authoritative: dict[str, Any]) -> None:
                self.authoritative = authoritative

        try:
            with immediate_transaction(conn, "complete_backup_creation_request"):
                complete_backup_creation(
                    conn, request_id=request_id, record=record
                )
                authoritative = complete_request(
                    conn, request_id=request_id, result=result
                )
                if not self._backup_result_matches_record(authoritative, record):
                    raise _AuthoritativeReplayWon(authoritative)
                return authoritative
        except _AuthoritativeReplayWon as replay:
            return replay.authoritative

    def create_backup(
        self,
        *,
        label: str = "manual",
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            principal = principal or self._default_principal({}, admin=True)
            replay_started = False
            try:
                conn = self._initialize_database(
                    surface="admin",
                    command="backup-create",
                    request_id=request_id,
                    principal=principal,
                )
            except Exception as exc:
                return error_envelope(
                    "backup-create", _database_initialization_error(exc)
                )
            try:
                manager = self.backup_manager
                creation = None
                if request_id:
                    row, replay_started = begin_request(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command="backup-create",
                        arguments={"label": label},
                    )
                    prior = stored_result(row)
                    if prior is not None:
                        return prior
                    creation = creation_for_request(conn, request_id)
                    if not replay_started:
                        if creation is None:
                            raise pending_error("backup-create", request_id)
                        recovered = manager.existing_record(creation["backup_id"])
                        if recovered is None:
                            exc = pending_error("backup-create", request_id)
                            exc.details.update({
                                "backup_id": creation["backup_id"],
                                "backup_identity_reserved": True,
                            })
                            raise exc
                        result = result_envelope(
                            command="backup-create",
                            data={
                                "backup": recovered.as_dict(),
                                "request_id": request_id,
                                "request_replayed": True,
                                "backup_recovered_from_interruption": True,
                            },
                        )
                        return self._commit_backup_creation_result(
                            conn,
                            request_id=request_id,
                            record=recovered,
                            result=result,
                        )

                    creation = reserve_backup_creation(
                        conn,
                        request_id=request_id,
                        backup_id=manager.new_backup_id(label=label),
                    )

                record = manager.create(
                    label=label,
                    backup_id=None if creation is None else creation["backup_id"],
                )
                result = result_envelope(
                    command="backup-create", data={"backup": record.as_dict()}
                )
                if request_id:
                    result.setdefault("data", {})["request_id"] = request_id
                    return self._commit_backup_creation_result(
                        conn, request_id=request_id, record=record, result=result
                    )
                return result
            except DishRuleError as exc:
                exc = _preserve_semantic_evidence_error(
                    exc,
                    execution_occurred=True,
                    request_id_consumed=bool(request_id and replay_started),
                )
                result = error_envelope("backup-create", exc)
                if request_id and replay_started:
                    result.setdefault("data", {})["request_id"] = request_id
                    complete_request(conn, request_id=request_id, result=result)
                return result
            except Exception as exc:
                result = error_envelope(
                    "backup-create",
                    _database_execution_unavailable_error(
                        exc,
                        request_id_consumed=bool(request_id and replay_started),
                    ),
                )
                if request_id:
                    result.setdefault("data", {})["request_id"] = request_id
                return result
            finally:
                conn.close()

    @staticmethod
    def _restore_result_requires_lockout(result: Mapping[str, Any]) -> bool:
        if result.get("ok"):
            return False
        errors = result.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else {}
        database_retained = first.get("database_retained")
        return bool(
            first.get("rule") == "backup_restore_and_rollback_failed"
            or database_retained is False
            or (
                result.get("code") == "BACKEND_UNCERTAIN"
                and database_retained is not True
            )
        )

    def _restore_checkpoint_writer(
        self,
        *,
        request_id: str | None,
        marker_context: Mapping[str, Any],
    ):
        def persist(stage: str, details: Mapping[str, Any]) -> None:
            # The detailed exact-effect plan belongs in the request journal. The
            # fault marker remains a small fail-closed locator exposed by health.
            if request_id:
                self._restore_requests.checkpoint(
                    request_id=request_id, stage=stage, details=details
                )
            try:
                self._restore_fault.set({
                    **dict(marker_context),
                    "kind": "backup_restore_in_progress",
                    "stage": stage,
                })
            except Exception:
                # The marker was durably pre-armed before restore execution. Its
                # stage enrichment is diagnostic only; the journal checkpoint is
                # the exact recovery authority and must not be undone or turned
                # into a second restore attempt because enrichment failed.
                pass

        return persist

    def _finalize_restore_result(
        self,
        *,
        request_id: str | None,
        backup_id: str,
        result: dict[str, Any],
        recovered_from_interruption: bool = False,
    ) -> dict[str, Any]:
        if request_id:
            result.setdefault("data", {})["request_id"] = request_id
            try:
                # The replay result commits before the lockout is cleared. A
                # kill in between therefore replays the committed result and
                # startup can safely remove only the stale marker.
                self._restore_requests.complete(
                    request_id=request_id,
                    result=result,
                    recovered_from_interruption=recovered_from_interruption,
                )
            except Exception as exc:
                result.setdefault("data", {})["service_recovery_required"] = True
                result["data"]["journal_error_type"] = type(exc).__name__
                return result

        if self._restore_result_requires_lockout(result):
            errors = result.get("errors")
            first = errors[0] if isinstance(errors, list) and errors else {}
            try:
                self._restore_fault.set({
                    "kind": "backup_restore_recovery_required",
                    "backup_id": str(backup_id),
                    "request_id": request_id,
                    "rule": first.get("rule"),
                    "details": {
                        key: value
                        for key, value in first.items()
                        if key not in {"message"}
                    },
                })
            except Exception:
                # The already-armed marker is the durable fail-closed record.
                pass
        else:
            try:
                self._restore_fault.clear()
            except Exception as exc:
                result.setdefault("data", {})["service_cleanup_warning"] = {
                    "kind": "restore_fault_marker_clear_failed",
                    "error_type": type(exc).__name__,
                    "do_not_retry": True,
                }
        return result

    def _execute_restore_locked(
        self,
        *,
        backup_id: str,
        request_id: str | None,
        marker_context: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None = None,
        recovered_from_interruption: bool = False,
    ) -> dict[str, Any]:
        manager = self.backup_manager
        manager.set_restore_checkpoint(
            self._restore_checkpoint_writer(
                request_id=request_id, marker_context=marker_context
            )
        )
        try:
            if checkpoint is None:
                data = manager.restore(backup_id)
            else:
                data = manager.recover_restore(backup_id, checkpoint)
                data = {
                    **data,
                    "restore_recovered": True,
                    "recovered_from_stage": checkpoint.get("stage"),
                }
            result = result_envelope(command="backup-restore", data=data)
        except DishRuleError as exc:
            exc = _preserve_semantic_evidence_error(
                exc,
                execution_occurred=True,
                request_id_consumed=bool(request_id),
            )
            result = error_envelope("backup-restore", exc)
        except Exception as exc:
            error = DishRuleError(
                "INTERNAL_ERROR",
                "database restore failed with an unclassified recovery outcome; "
                "workflow mutations are disabled",
                rule="backup_restore_recovery_unknown",
                details={
                    "error_type": type(exc).__name__,
                    "database_retained": False,
                },
            )
            result = error_envelope("backup-restore", error)
        finally:
            manager.set_restore_checkpoint(None)
        return self._finalize_restore_result(
            request_id=request_id,
            backup_id=backup_id,
            result=result,
            recovered_from_interruption=recovered_from_interruption,
        )

    def _recover_interrupted_restore_locked(self) -> dict[str, Any] | None:
        marker = self._restore_fault.read()
        marker_present = isinstance(marker, dict)
        if marker_present and marker.get("kind") != "backup_restore_in_progress":
            return None

        if marker_present:
            request_id = str(marker.get("request_id") or "").strip()
            backup_id = str(marker.get("backup_id") or "").strip()
            if not request_id or not backup_id:
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "restore recovery marker is incomplete; do not repeat the restore",
                    rule="restore_recovery_marker_invalid",
                    retryable=False,
                    details={},
                )
            row = self._restore_requests.read(request_id)
            if row is None:
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "restore recovery journal entry is missing; do not repeat the restore",
                    rule="restore_request_journal_missing",
                    retryable=False,
                    details={"request_id": request_id},
                )
        else:
            row = self._restore_requests.pending_restore()
            if row is None:
                return None
            request_id = str(row.get("request_id") or "").strip()
            arguments = row.get("arguments")
            backup_id = (
                str(arguments.get("backup_id") or "").strip()
                if isinstance(arguments, dict)
                else ""
            )
            if not request_id or not backup_id:
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "restore request journal is incomplete; do not repeat the restore",
                    rule="restore_request_journal_invalid",
                    retryable=False,
                    details={"request_id": request_id or None},
                )

        row_arguments = row.get("arguments")
        journal_backup_id = (
            str(row_arguments.get("backup_id") or "").strip()
            if isinstance(row_arguments, dict)
            else ""
        )
        if (
            row.get("command") != "backup-restore"
            or not journal_backup_id
            or journal_backup_id != backup_id
        ):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore marker and request journal disagree; do not repeat the restore",
                rule="restore_recovery_identity_mismatch",
                retryable=False,
                details={"request_id": request_id},
            )

        prior = self._restore_requests.stored_result(row)
        if prior is not None:
            # A crash after terminal journal completion but before marker cleanup
            # is still an interrupted restore. Preserve that fact before clearing
            # the only startup locator so one fresh client UUID can replay it.
            self._restore_requests.mark_recovered_from_interruption(
                request_id=request_id
            )
            # stored_result adds replay metadata, which is suitable for a later
            # exact retry and harmless in the startup recovery summary.
            if not self._restore_result_requires_lockout(prior):
                try:
                    self._restore_fault.clear()
                except Exception:
                    pass
            return prior

        checkpoint = self._restore_requests.last_checkpoint(row)
        if checkpoint is None:
            raise pending_error("backup-restore", request_id)

        marker_context = {
            "kind": "backup_restore_in_progress",
            "stage": checkpoint.get("stage") or "request_accepted",
            "backup_id": backup_id,
            "request_id": request_id,
            "owner_id": row.get("owner_id"),
            "run_id": row.get("run_id"),
        }
        if not marker_present:
            try:
                # The exact-effect journal can outlive a crash before the small
                # locator was written. Re-arm the fail-closed marker before any
                # recovery mutation is attempted.
                self._restore_fault.set(marker_context)
            except Exception as marker_exc:
                error = DishRuleError(
                    "INTERNAL_ERROR",
                    "restore lockout could not be persisted; the database was not replaced",
                    rule="restore_lockout_persistence_failed",
                    details={
                        "error_type": type(marker_exc).__name__,
                        "database_retained": True,
                    },
                )
                return self._finalize_restore_result(
                    request_id=request_id,
                    backup_id=backup_id,
                    result=error_envelope("backup-restore", error),
                    recovered_from_interruption=True,
                )

        return self._execute_restore_locked(
            backup_id=backup_id,
            request_id=request_id,
            marker_context=marker_context,
            checkpoint=checkpoint,
            recovered_from_interruption=True,
        )

    def restore_backup(
        self,
        backup_id: str,
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_gate.restore():
            principal = principal or self._default_principal({}, admin=True)
            request_id = request_id or str(uuid.uuid4())
            arguments = {"backup_id": backup_id}

            # Reconcile exact interrupted work before accepting another restore.
            # The journal remains discoverable even when the process died before
            # the small fault-marker locator was written.
            try:
                recovered = self._recover_interrupted_restore_locked()
            except DishRuleError as exc:
                result = error_envelope("backup-restore", exc)
                result.setdefault("data", {})["request_id"] = request_id
                return result
            if recovered is not None:
                recovered_data = recovered.get("data")
                recovered_backup = (
                    recovered_data.get("restored", {}).get("source_backup_id")
                    if isinstance(recovered_data, dict)
                    and isinstance(recovered_data.get("restored"), dict)
                    else None
                )
                recovered_request_id = (
                    str(recovered_data.get("request_id") or "")
                    if isinstance(recovered_data, dict)
                    else ""
                )
                if request_id == recovered_request_id:
                    return recovered
                if recovered_backup == str(backup_id):
                    alias = self._restore_requests.claim_recovered_result(
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command="backup-restore",
                        arguments=arguments,
                    )
                    if alias is not None:
                        return alias
                elif not recovered.get("ok"):
                    # A failed or uncertain recovery must be surfaced before a
                    # different restore can be considered.
                    return recovered

            # Startup may already have completed and cleared an interrupted
            # restore. Bind the first fresh UUID for the same backup to that
            # recovered result before creating any new pending work.
            try:
                alias = self._restore_requests.claim_recovered_result(
                    request_id=request_id,
                    owner_id=principal.owner_id,
                    run_id=principal.run_id,
                    command="backup-restore",
                    arguments=arguments,
                )
                if alias is not None:
                    return alias
            except DishRuleError as exc:
                result = error_envelope("backup-restore", exc)
                result.setdefault("data", {})["request_id"] = request_id
                return result

            checkpoint = None
            try:
                row, replay_started = self._restore_requests.begin(
                    request_id=request_id,
                    owner_id=principal.owner_id,
                    run_id=principal.run_id,
                    command="backup-restore",
                    arguments=arguments,
                )
                prior = self._restore_requests.stored_result(row)
                if prior is not None:
                    return prior
                if not replay_started:
                    checkpoint = self._restore_requests.last_checkpoint(row)
                    if checkpoint is None:
                        raise pending_error("backup-restore", request_id)
            except DishRuleError as exc:
                result = error_envelope("backup-restore", exc)
                result.setdefault("data", {})["request_id"] = request_id
                return result

            marker_context = {
                "kind": "backup_restore_in_progress",
                "stage": "armed",
                "backup_id": str(backup_id),
                "request_id": request_id,
                "owner_id": principal.owner_id,
                "run_id": principal.run_id,
            }
            try:
                self._restore_fault.set(marker_context)
            except Exception as marker_exc:
                error = DishRuleError(
                    "INTERNAL_ERROR",
                    "restore lockout could not be persisted; the database was not replaced",
                    rule="restore_lockout_persistence_failed",
                    details={
                        "error_type": type(marker_exc).__name__,
                        "database_retained": True,
                    },
                )
                result = error_envelope("backup-restore", error)
                return self._finalize_restore_result(
                    request_id=request_id,
                    backup_id=backup_id,
                    result=result,
                )

            return self._execute_restore_locked(
                backup_id=backup_id,
                request_id=request_id,
                marker_context=marker_context,
                checkpoint=checkpoint,
                recovered_from_interruption=checkpoint is not None,
            )

    def record_restore_validation_failure(
        self,
        *,
        backup_id: str,
        principal: ServicePrincipal,
        request_id: str,
        error: DishRuleError,
    ) -> dict[str, Any]:
        """Persist accepted restore request failures outside the live database."""
        with self._maintenance_gate.restore():
            row, started = self._restore_requests.begin(
                request_id=request_id,
                owner_id=principal.owner_id,
                run_id=principal.run_id,
                command="backup-restore",
                arguments={"backup_id": backup_id},
            )
            prior = self._restore_requests.stored_result(row)
            if prior is not None:
                return prior
            if not started:
                raise pending_error("backup-restore", request_id)
            result = error_envelope("backup-restore", error)
            result.setdefault("data", {})["request_id"] = request_id
            self._restore_requests.complete(request_id=request_id, result=result)
            return result

    def startup_check(self) -> dict[str, Any]:
        """Report startup state and reconcile a durably checkpointed restore."""
        repaired = 0
        release = None
        database_initialization_error_type = None
        audit_repair_error_type = None
        compatibility_error_type = None
        restore_recovery_result: dict[str, Any] | None = None
        restore_recovery_error_type = None
        planning_reopen_recovery: dict[str, Any] = {
            "discovered": 0, "confirmed": 0, "not_applied": 0,
            "resume_safe": 0, "applied_pending_replay": 0,
            "uncertain": 0, "pending": [], "errors": [],
        }
        try:
            with self._maintenance_gate.restore():
                restore_recovery_result = self._recover_interrupted_restore_locked()
        except Exception as exc:
            restore_recovery_error_type = type(exc).__name__

        try:
            conn = self._initialize_database(surface="startup", command="startup-check")
        except Exception as exc:
            database_initialization_error_type = type(exc).__name__
            conn = None
        if conn is not None:
            try:
                try:
                    repaired = process_command_audit_repairs(conn)
                except Exception as exc:
                    audit_repair_error_type = type(exc).__name__
                try:
                    planning_reopen_recovery = (
                        self._reconcile_planning_reopens_startup(conn)
                    )
                except Exception as exc:
                    planning_reopen_recovery["errors"].append({
                        "startup_error_type": type(exc).__name__,
                    })
            finally:
                conn.close()
        try:
            release = self._release(None)
        except Exception as exc:
            compatibility_error_type = type(exc).__name__

        result = self.health()
        startup = result.setdefault("startup", {})
        startup["audit_repairs_processed"] = repaired
        startup["protocol_version"] = None if release is None else release.protocol_version
        startup["schema_version"] = None if release is None else release.schema_version
        startup["database_initialization_error_type"] = database_initialization_error_type
        startup["audit_repair_error_type"] = audit_repair_error_type
        startup["compatibility_error_type"] = compatibility_error_type
        recovery_summary: dict[str, Any] = {
            "attempted": restore_recovery_result is not None,
            "error_type": restore_recovery_error_type,
        }
        if restore_recovery_result is not None:
            errors = restore_recovery_result.get("errors")
            first = errors[0] if isinstance(errors, list) and errors else {}
            data = restore_recovery_result.get("data")
            recovery_summary.update({
                "ok": bool(restore_recovery_result.get("ok")),
                "code": restore_recovery_result.get("code"),
                "rule": first.get("rule") if isinstance(first, dict) else None,
                "request_id": (
                    data.get("request_id") if isinstance(data, dict) else None
                ),
            })
        startup["restore_recovery"] = recovery_summary
        startup["planning_reopen_recovery"] = planning_reopen_recovery
        # Configuration is the listener-start boundary. Database, compatibility,
        # Asana, and restore faults leave the process available for diagnosis,
        # lease recovery, backup attempts, and administrative restore.
        result["startup_ready"] = bool(result["configuration"].get("ok"))
        return result

    def health(self) -> dict[str, Any]:
        with self._maintenance_gate.request():
            configuration: dict[str, Any]
            database: dict[str, Any]
            compatibility: dict[str, Any]
            asana: dict[str, Any]
            audit: dict[str, Any] = {"pending_repairs": None}
            operations: dict[str, Any] = {"active": None}
            leases: dict[str, Any] = {"active": None, "expired": None}

            try:
                self.config.validate_runtime(require_action=False)
                configuration = {"ok": True}
            except DishRuleError as exc:
                configuration = {"ok": False, "rule": exc.rule, "message": str(exc)}
            except Exception as exc:
                configuration = {"ok": False, "rule": "service_config_invalid", "message": type(exc).__name__}

            conn = None
            try:
                conn = self._initialize_database(surface="health", command="health")
                _probe_database_write_readiness(conn)
                database = {"ok": True, "schema_version": SCHEMA_VERSION, "write_ready": True}
                audit["pending_repairs"] = conn.execute(
                    "SELECT COUNT(*) FROM command_audit_repairs WHERE repaired_at IS NULL"
                ).fetchone()[0]
                operations["active"] = conn.execute(
                    "SELECT COUNT(*) FROM operations WHERE status IN ('open','uncertain')"
                ).fetchone()[0]
                leases["active"] = conn.execute(
                    "SELECT COUNT(*) FROM service_leases WHERE released_at IS NULL"
                ).fetchone()[0]
                leases["expired"] = conn.execute(
                    "SELECT COUNT(*) FROM service_leases WHERE released_at IS NULL AND expires_at <= ?",
                    (_now_stamp(),),
                ).fetchone()[0]
            except DishRuleError as exc:
                database = {"ok": False, "rule": exc.rule, "message": str(exc), "write_ready": False}
            except Exception as exc:
                database = {"ok": False, "rule": "database_health_failed", "message": type(exc).__name__, "write_ready": False}
            finally:
                if conn is not None:
                    conn.close()

            try:
                release = self._release(None)
                compatibility = {
                    "ok": True,
                    "protocol_version": release.protocol_version,
                    "schema_version": release.schema_version,
                }
            except DishRuleError as exc:
                compatibility = {"ok": False, "rule": exc.rule, "message": str(exc)}
            except Exception as exc:
                compatibility = {"ok": False, "rule": "compatibility_health_failed", "message": type(exc).__name__}

            backend = None
            try:
                backend = self.backend_factory()
                registry = SectionRegistry.from_sections(
                    backend.list_sections(COOKING_PROJECT_GID)
                )
                asana = {
                    "ok": True,
                    "required_sections": {
                        "research_queue": registry.research_queue_gid,
                        "verification_queue": registry.verification_queue_gid,
                    },
                }
            except DishRuleError as exc:
                asana = {"ok": False, "rule": exc.rule, "message": str(exc)}
            except Exception as exc:
                asana = {"ok": False, "rule": "asana_health_failed", "message": type(exc).__name__}
            finally:
                self._close_backend(backend)

            restore_fault = self._restore_fault.read()
            maintenance = {
                "ok": restore_fault is None,
                "restore_recovery_required": restore_fault is not None,
                "restore_fault": restore_fault,
            }
            ok = bool(
                configuration.get("ok")
                and database.get("ok")
                and compatibility.get("ok")
                and asana.get("ok")
                and maintenance["ok"]
            )
            return {
                "ok": ok,
                "service": "dish",
                "configuration": configuration,
                "database": database,
                "compatibility": compatibility,
                "asana": asana,
                "maintenance": maintenance,
                "audit": audit,
                "operations": operations,
                "leases": leases,
            }
