"""Transport-neutral shared-service boundary around the existing applications."""
from __future__ import annotations

import contextlib
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_tool.admin import DishAdminApplication
from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID, SCHEMA_VERSION
from dish_tool.database import initialize_database, process_command_audit_repairs
from dish_tool.errors import DishRuleError
from dish_tool.models import SectionRegistry
from dish_tool.releases import resolve_release
from dish_tool.results import error_envelope, result_envelope

from .backup import BackupManager
from .config import ServiceConfig
from .leases import LeaseManager, ServicePrincipal

_READ_ONLY_AGENT_COMMANDS = {"sections", "read", "inspect"}
_LEASED_AGENT_COMMANDS = {"prepare", "approve", "reject", "submit"}
_HANDOFF_PHASES = {"await_verification", "held_evidence", "held_human"}
_OPERATION_ADMIN_COMMANDS = {
    "recover",
    "discard",
    "reopen",
    "supply-evidence",
    "record-human-decision",
    "authorize-governed-change",
}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DishService:
    """Shared persistent authority around the existing workflow applications.

    Every request still gets a fresh SQLite connection, but one in-process
    maintenance lock serializes database replacement and workflow mutations. The
    durable operations/task constraint and service leases remain the cross-request
    authority; the lock only ensures a restore cannot overlap a request.
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
        self.backend_factory = backend_factory or AsanaBackend
        self.release_loader = release_loader
        self.lease_now = lease_now
        self._maintenance_lock = threading.RLock()
        self._restore_faulted = False

    @property
    def backup_manager(self) -> BackupManager:
        backup_dir = self.config.backup_dir or (self.config.db_path.parent / "backups")
        return BackupManager(self.config.db_path, backup_dir)

    def _release(self, role: str | None = None, *, include_migrations: bool = False):
        if self.release_loader is not None:
            try:
                return self.release_loader(role, include_migrations=include_migrations)
            except TypeError:
                try:
                    return self.release_loader(role)
                except TypeError:
                    return self.release_loader()
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
        if command == "start" and arguments.get("kind") == "verification":
            task_gid = str(arguments.get("task_gid") or "").strip()
            row = conn.execute(
                "SELECT operation_id FROM operations WHERE task_gid=? AND status IN ('open','uncertain') ORDER BY created_at DESC LIMIT 1",
                (task_gid,),
            ).fetchone()
            return None if row is None else row["operation_id"]
        return None

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

    def _assert_mutation_ready(self, backend: Any) -> None:
        # Compatibility is resolved before any workflow mutation. Asana access is
        # proven with the same read-only section registry contract used by the
        # workflow itself; malformed/missing queues fail closed. A restore whose
        # rollback could not be proven keeps this process diagnosis-only.
        if self._restore_faulted:
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
                if op["status"] in {"completed", "cancelled"}:
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
            data["service_recovery_required"] = True
            data["service_recovery"] = {
                "kind": "lease_finalization",
                "operation_id": operation_id,
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
                data["service_recovery"]["lease_read_error_type"] = type(lease_read_exc).__name__
            result["allowed_actions"] = []
            result["retryable"] = False
        return result

    def execute_agent(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_lock:
            conn = initialize_database(self.config.db_path)
            acquired_for_request = False
            operation_id = None
            principal = principal or self._default_principal(arguments)
            leases = self._lease_manager(conn)
            try:
                backend = self.backend_factory()
                if command not in _READ_ONLY_AGENT_COMMANDS:
                    self._assert_mutation_ready(backend)
                app = DishApplication(
                    conn,
                    backend,
                    release_loader=lambda role=None: self._release(role),
                )
                operation_id = self._operation_for_request(conn, command, arguments)
                if command in _LEASED_AGENT_COMMANDS:
                    if not operation_id:
                        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
                    leases.assert_owned(operation_id, principal)
                elif command == "start" and arguments.get("kind") == "verification":
                    if not operation_id:
                        raise DishRuleError("NOT_FOUND", "task has no open operation", rule="open_operation_missing")
                    leases.acquire(operation_id, principal)
                    acquired_for_request = True

                with self._candidate_file(arguments) as prepared:
                    result = app.execute(command, **prepared)

                if command == "start" and arguments.get("kind") != "verification" and result.get("ok"):
                    operation_id = result.get("submission_id")
                    if operation_id:
                        leases.acquire(operation_id, principal)
                        acquired_for_request = True

                if result.get("ok") and operation_id:
                    result = self._finalize_successful_lease(
                        result=result,
                        conn=conn,
                        leases=leases,
                        operation_id=operation_id,
                        principal=principal,
                        command=command,
                    )
                elif not result.get("ok") and acquired_for_request and operation_id:
                    if command == "start" and arguments.get("kind") == "verification":
                        leases.release(operation_id, principal, reason="verification_start_failed")
                return result
            except DishRuleError as exc:
                if acquired_for_request and operation_id:
                    try:
                        leases.release(operation_id, principal, reason="service_command_rejected")
                    except Exception:
                        pass
                return error_envelope(command, exc, submission_id=operation_id)
            finally:
                conn.close()

    def renew_lease(self, operation_id: str, principal: ServicePrincipal) -> dict[str, Any]:
        with self._maintenance_lock:
            conn = initialize_database(self.config.db_path)
            try:
                row = self._lease_manager(conn).renew(operation_id, principal)
                return result_envelope(
                    command="renew-lease",
                    submission_id=operation_id,
                    data={"service_lease": self._lease_payload(row)},
                )
            except DishRuleError as exc:
                return error_envelope("renew-lease", exc, submission_id=operation_id)
            finally:
                conn.close()

    def recover_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
    ) -> dict[str, Any]:
        with self._maintenance_lock:
            conn = initialize_database(self.config.db_path)
            try:
                row = self._lease_manager(conn).admin_recover(operation_id, principal, reason=reason)
                return result_envelope(
                    command="recover-lease",
                    submission_id=operation_id,
                    data={"service_lease": self._lease_payload(row)},
                )
            except DishRuleError as exc:
                return error_envelope("recover-lease", exc, submission_id=operation_id)
            finally:
                conn.close()

    def record_agent_argument_failure(
        self,
        command: str,
        error_payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._maintenance_lock:
            conn = initialize_database(self.config.db_path)
            try:
                app = DishApplication(
                    conn,
                    self.backend_factory(),
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
                conn.close()

    def record_admin_argument_failure(
        self,
        command: str,
        error_payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._maintenance_lock:
            conn = initialize_database(self.config.db_path)
            try:
                app = DishAdminApplication(
                    conn,
                    backend=self.backend_factory(),
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
                conn.close()

    def execute_admin(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_lock:
            conn = initialize_database(self.config.db_path)
            principal = principal or self._default_principal(arguments, admin=True)
            operation_id = str(arguments.get("submission_id") or "").strip() or None
            acquired_for_request = False
            leases = self._lease_manager(conn)
            try:
                backend = self.backend_factory()
                self._assert_mutation_ready(backend)
                if command in _OPERATION_ADMIN_COMMANDS and operation_id:
                    existing = leases.active_for_operation(operation_id)
                    if existing is None:
                        leases.acquire(operation_id, principal)
                        acquired_for_request = True
                    else:
                        leases.assert_owned(operation_id, principal)
                app = DishAdminApplication(
                    conn,
                    backend=backend,
                    release_loader=lambda: self._release(None, include_migrations=True),
                )
                with self._candidate_file(arguments) as prepared:
                    result = app.execute(command, **prepared)
                if result.get("ok") and operation_id:
                    result = self._finalize_successful_lease(
                        result=result,
                        conn=conn,
                        leases=leases,
                        operation_id=operation_id,
                        principal=principal,
                        command=command,
                        admin=True,
                    )
                elif not result.get("ok") and acquired_for_request and operation_id:
                    leases.release(operation_id, principal, reason="admin_command_rejected")
                return result
            except DishRuleError as exc:
                if acquired_for_request and operation_id:
                    try:
                        leases.release(operation_id, principal, reason="admin_command_rejected")
                    except Exception:
                        pass
                return error_envelope(command, exc, submission_id=operation_id)
            finally:
                conn.close()

    def create_backup(self, *, label: str = "manual") -> dict[str, Any]:
        with self._maintenance_lock:
            try:
                record = self.backup_manager.create(label=label)
                return result_envelope(command="backup-create", data={"backup": record.as_dict()})
            except DishRuleError as exc:
                return error_envelope("backup-create", exc)

    def restore_backup(self, backup_id: str) -> dict[str, Any]:
        with self._maintenance_lock:
            try:
                data = self.backup_manager.restore(backup_id)
                self._restore_faulted = False
                return result_envelope(command="backup-restore", data=data)
            except DishRuleError as exc:
                if exc.rule == "backup_restore_and_rollback_failed":
                    self._restore_faulted = True
                return error_envelope("backup-restore", exc)
            except Exception as exc:
                self._restore_faulted = True
                error = DishRuleError(
                    "INTERNAL_ERROR",
                    "database restore failed with an unclassified recovery outcome; "
                    "workflow mutations are disabled",
                    rule="backup_restore_recovery_unknown",
                    details={"error_type": type(exc).__name__},
                )
                return error_envelope("backup-restore", error)

    def startup_check(self) -> dict[str, Any]:
        """Validate durable state and repair pending invocation audits before serving."""
        with self._maintenance_lock:
            conn = initialize_database(self.config.db_path)
            try:
                repaired = process_command_audit_repairs(conn)
                release = self._release(None)
            finally:
                conn.close()
            result = self.health()
            result.setdefault("startup", {})["audit_repairs_processed"] = repaired
            result["startup"]["protocol_version"] = release.protocol_version
            result["startup"]["schema_version"] = release.schema_version
            return result

    def health(self) -> dict[str, Any]:
        with self._maintenance_lock:
            database: dict[str, Any]
            compatibility: dict[str, Any]
            asana: dict[str, Any]
            audit: dict[str, Any] = {"pending_repairs": None}
            operations: dict[str, Any] = {"active": None}
            leases: dict[str, Any] = {"active": None, "expired": None}

            conn = None
            try:
                conn = initialize_database(self.config.db_path)
                database = {"ok": True, "schema_version": SCHEMA_VERSION}
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
                database = {"ok": False, "rule": exc.rule, "message": str(exc)}
            except Exception as exc:
                database = {"ok": False, "rule": "database_health_failed", "message": type(exc).__name__}
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

            try:
                registry = SectionRegistry.from_sections(
                    self.backend_factory().list_sections(COOKING_PROJECT_GID)
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

            maintenance = {
                "ok": not self._restore_faulted,
                "restore_recovery_required": self._restore_faulted,
            }
            ok = bool(
                database.get("ok")
                and compatibility.get("ok")
                and asana.get("ok")
                and maintenance["ok"]
            )
            return {
                "ok": ok,
                "service": "dish",
                "database": database,
                "compatibility": compatibility,
                "asana": asana,
                "maintenance": maintenance,
                "audit": audit,
                "operations": operations,
                "leases": leases,
            }
