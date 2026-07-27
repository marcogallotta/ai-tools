"""Transport-neutral shared-service boundary around the existing applications."""
from __future__ import annotations

import contextlib
import tempfile
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
from dish_tool.step5 import diagnostics_for, start_result_data
from dish_tool.step7 import replay_verification_read
from dish_tool.task_store import read_complete_task
from dish_tool.releases import resolve_release
from dish_tool.results import error_envelope, result_envelope
from dish_tool.validation_scope import scope_for_command

from .backup import BackupManager
from .config import ServiceConfig
from .leases import LeaseManager, ServicePrincipal
from .maintenance import MaintenanceGate
from .request_replay import begin_request, complete_request, pending_error, stored_result
from .restore_fault import RestoreFaultMarker

_READ_ONLY_AGENT_COMMANDS = {"sections", "read", "inspect"}
_LEASED_AGENT_COMMANDS = {"prepare", "approve", "reject", "submit"}
_MUTATING_AGENT_COMMANDS = {"create", "start", *_LEASED_AGENT_COMMANDS}
_RUN_ID_AGENT_COMMANDS = {"start", "prepare", "approve", "reject"}
_HANDOFF_PHASES = {"await_verification", "held_evidence", "held_human"}
_OPERATION_ADMIN_COMMANDS = {
    "recover",
    "discard",
    "reopen",
    "supply-evidence",
    "record-human-decision",
    "authorize-governed-change",
}
_LEASE_FREE_ADMIN_COMMANDS = {"authorize-governed-change"}


def _now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DishService:
    """Shared persistent authority around the existing workflow applications.

    Every request gets a fresh SQLite connection. Ordinary requests may run
    concurrently, while an in-process maintenance gate gives database replacement
    exclusive access. Durable operation constraints and service leases remain the
    cross-request workflow authority.
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
        self._restore_fault = RestoreFaultMarker(self.config.db_path)

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

    def _close_backend(self, backend: Any | None) -> None:
        if backend is None or not self._owns_backend_instances:
            return
        close = getattr(backend, "close", None)
        if callable(close):
            close()

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
    def _arguments_for_principal(
        command: str,
        arguments: Mapping[str, Any],
        *,
        run_id: str | None,
    ) -> dict[str, Any]:
        prepared = dict(arguments)
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
    def _operation_row(conn, operation_id: str):
        return conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()

    @staticmethod
    def _run_has_role(conn, operation_id: str, run_id: str, roles: tuple[str, ...]) -> bool:
        placeholders = ",".join("?" for _ in roles)
        row = conn.execute(
            f"SELECT 1 FROM operation_actor_facts "
            f"WHERE operation_id=? AND run_id=? AND role IN ({placeholders}) LIMIT 1",
            (operation_id, run_id, *roles),
        ).fetchone()
        return row is not None

    def _may_claim_missing_lease(
        self, conn, operation_id: str, principal: ServicePrincipal, command: str
    ) -> bool:
        op = self._operation_row(conn, operation_id)
        if op is None or op["status"] != "open":
            return False
        if command == "prepare":
            if str(op["run_id"] or "").strip() == principal.run_id:
                return True
            return self._run_has_role(
                conn, operation_id, principal.run_id,
                ("planner", "constructor", "material_editor"),
            )
        if command in {"approve", "reject", "submit"}:
            return self._run_has_role(
                conn, operation_id, principal.run_id, ("verifier",)
            )
        return False

    def _apply_principal_access(
        self,
        result: dict[str, Any],
        *,
        conn,
        leases: LeaseManager,
        operation_id: str | None,
        principal: ServicePrincipal,
    ) -> dict[str, Any]:
        if not result.get("ok") or not operation_id:
            return result
        op = self._operation_row(conn, operation_id)
        if op is None:
            return result
        data = result.setdefault("data", {})
        active = leases.active_for_operation(operation_id)
        data["service_lease"] = self._lease_payload(active)
        actions = list(result.get("allowed_actions") or [])

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
            actions = []
            access = {"state": "terminal"}
        elif active is not None:
            if leases.is_expired(active):
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
                    conn, operation_id, principal, action
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

        result["allowed_actions"] = actions
        data["service_access"] = access
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
        rows = conn.execute(
            """SELECT * FROM operations
                 WHERE task_gid=? AND status IN ('open','uncertain')
                 ORDER BY created_at DESC""",
            (task_gid,),
        ).fetchall()
        if len(rows) != 1 or rows[0]["status"] != "open":
            raise pending_error("start", request_id)
        operation = rows[0]
        operation_id = operation["operation_id"]

        if kind == "verification":
            data = replay_verification_read(
                conn, backend, operation_id=operation_id,
                agent=str(arguments.get("agent") or ""), run_id=principal.run_id,
            )
        else:
            if (
                operation["operation_kind"] != kind
                or str(operation["run_id"] or "").strip() != principal.run_id
                or operation["phase"] != "prepare_required"
            ):
                raise pending_error("start", request_id, operation_id=operation_id)
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
            leases.acquire(operation_id, principal)
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
        result = result_envelope(
            command="start",
            task_gid=task_gid,
            submission_id=operation_id,
            state=operation["status"],
            allowed_actions=inspected.get("allowed_actions", []),
            data=data,
        )
        result = self._apply_principal_access(
            result, conn=conn, leases=leases, operation_id=operation_id,
            principal=principal,
        )
        complete_request(conn, request_id=request_id, result=result)
        return result

    def execute_agent(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            conn = initialize_database(self.config.db_path)
            backend = None
            acquired_for_request = False
            operation_id = None
            replay_started = False
            explicit_principal = principal is not None
            principal = principal or self._default_principal(arguments)
            invocation_run_id = (
                principal.run_id
                if explicit_principal
                else str(arguments.get("run_id") or "").strip() or None
            )
            leases = self._lease_manager(conn)
            try:
                prepared_arguments = self._arguments_for_principal(
                    command, arguments, run_id=invocation_run_id,
                )

                request_row = None
                if command in {"create", "start"} and request_id:
                    request_row, replay_started = begin_request(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command=command,
                        arguments=prepared_arguments,
                    )
                    prior = stored_result(request_row)
                    if prior is not None:
                        return prior

                backend = self.backend_factory()
                if command not in _READ_ONLY_AGENT_COMMANDS:
                    self._assert_mutation_ready(backend)
                app = DishApplication(
                    conn,
                    backend,
                    release_loader=lambda role=None: self._release(role),
                    invocation_run_id=invocation_run_id,
                )

                # A prior process may have committed start before it could persist
                # the result envelope. Reconcile only from exact durable workflow
                # and live-state evidence; otherwise fail uncertain.
                if request_row is not None and not replay_started:
                    if command == "start":
                        return self._reconcile_pending_start(
                            conn=conn, backend=backend, app=app, leases=leases,
                            principal=principal, arguments=prepared_arguments,
                            request_id=request_id,
                        )
                    raise pending_error(command, request_id)

                operation_id = self._operation_for_request(
                    conn, command, prepared_arguments,
                )
                if command in _LEASED_AGENT_COMMANDS:
                    if not operation_id:
                        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
                    active = leases.active_for_operation(operation_id)
                    if active is None:
                        if not self._may_claim_missing_lease(
                            conn, operation_id, principal, command
                        ):
                            raise DishRuleError(
                                "AGENT_MISMATCH",
                                "operation has no lease and this run has no durable workflow ownership",
                                rule="service_lease_claim_forbidden",
                                details={"operation_id": operation_id, "run_id": principal.run_id},
                            )
                        leases.acquire(operation_id, principal)
                        acquired_for_request = True
                    else:
                        leases.assert_owned(operation_id, principal)
                elif command == "start" and prepared_arguments.get("kind") == "verification":
                    if not operation_id:
                        raise DishRuleError("NOT_FOUND", "task has no open operation", rule="open_operation_missing")
                    active = leases.active_for_operation(operation_id)
                    if active is None:
                        leases.acquire(operation_id, principal)
                        acquired_for_request = True
                    else:
                        leases.assert_owned(operation_id, principal)

                with self._candidate_file(prepared_arguments) as prepared:
                    result = app.execute(command, **prepared)

                if command == "start" and prepared_arguments.get("kind") != "verification" and result.get("ok"):
                    operation_id = result.get("submission_id")
                    if operation_id:
                        try:
                            leases.acquire(operation_id, principal)
                            acquired_for_request = True
                        except Exception as exc:
                            data = result.setdefault("data", {})
                            data["service_recovery_required"] = True
                            data["service_recovery"] = {
                                "kind": "lease_acquisition",
                                "operation_id": operation_id,
                                "error_type": type(exc).__name__,
                                "do_not_retry_command": True,
                            }
                            result["allowed_actions"] = []
                            result["retryable"] = False

                result_operation_id = operation_id or result.get("submission_id")
                if result.get("ok") and result_operation_id and command in _MUTATING_AGENT_COMMANDS:
                    result = self._finalize_successful_lease(
                        result=result,
                        conn=conn,
                        leases=leases,
                        operation_id=result_operation_id,
                        principal=principal,
                        command=command,
                    )
                elif not result.get("ok") and acquired_for_request and operation_id:
                    if command == "start" and prepared_arguments.get("kind") == "verification":
                        leases.release(operation_id, principal, reason="verification_start_failed")
                    elif command in _LEASED_AGENT_COMMANDS:
                        leases.release(operation_id, principal, reason="reclaimed_command_rejected")
                result = self._apply_principal_access(
                    result,
                    conn=conn,
                    leases=leases,
                    operation_id=result_operation_id,
                    principal=principal,
                )
                if result.get("data", {}).get("service_recovery_required"):
                    result["allowed_actions"] = []
                if request_id and command in {"create", "start"}:
                    result.setdefault("data", {})["request_id"] = request_id
                    complete_request(conn, request_id=request_id, result=result)
                return result
            except DishRuleError as exc:
                if acquired_for_request and operation_id:
                    try:
                        leases.release(operation_id, principal, reason="service_command_rejected")
                    except Exception:
                        pass
                if operation_id is None:
                    operation_id = str(arguments.get("submission_id") or "").strip() or None
                operation_kind = None
                task_gid = None
                if operation_id:
                    row = conn.execute(
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
                    exc,
                    task_gid=task_gid,
                    submission_id=operation_id,
                    validation_scope=validation_scope,
                )
                if request_id and command in {"create", "start"} and replay_started:
                    result.setdefault("data", {})["request_id"] = request_id
                    complete_request(conn, request_id=request_id, result=result)
                return result
            finally:
                self._close_backend(backend)
                conn.close()

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
            conn = initialize_database(self.config.db_path)
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

    def renew_lease(self, operation_id: str, principal: ServicePrincipal) -> dict[str, Any]:
        with self._maintenance_gate.request():
            conn = initialize_database(self.config.db_path)
            try:
                row = self._lease_manager(conn).renew(operation_id, principal)
                operation = conn.execute(
                    "SELECT task_gid FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return result_envelope(
                    command="renew-lease",
                    task_gid=None if operation is None else operation["task_gid"],
                    submission_id=operation_id,
                    data={"service_lease": self._lease_payload(row)},
                )
            except DishRuleError as exc:
                row = conn.execute(
                    "SELECT task_gid FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return error_envelope(
                    "renew-lease",
                    exc,
                    task_gid=None if row is None else row["task_gid"],
                    submission_id=operation_id,
                )
            finally:
                conn.close()

    def recover_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            conn = initialize_database(self.config.db_path)
            try:
                released = self._lease_manager(conn).admin_recover(
                    operation_id, principal, reason=reason
                )
                return result_envelope(
                    command="recover-lease",
                    submission_id=operation_id,
                    data={
                        "service_lease": None,
                        "released_lease_id": None if released is None else released["lease_id"],
                        "ownership_transferred": False,
                    },
                )
            except DishRuleError as exc:
                row = conn.execute(
                    "SELECT task_gid FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return error_envelope(
                    "recover-lease",
                    exc,
                    task_gid=None if row is None else row["task_gid"],
                    submission_id=operation_id,
                )
            finally:
                conn.close()

    def record_agent_argument_failure(
        self,
        command: str,
        error_payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            conn = initialize_database(self.config.db_path)
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
            conn = initialize_database(self.config.db_path)
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

    def execute_admin(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            conn = initialize_database(self.config.db_path)
            backend = None
            principal = principal or self._default_principal(arguments, admin=True)
            operation_id = str(arguments.get("submission_id") or "").strip() or None
            acquired_for_request = False
            leases = self._lease_manager(conn)
            try:
                backend = self.backend_factory()
                self._assert_mutation_ready(backend)
                if (
                    command in _OPERATION_ADMIN_COMMANDS
                    and command not in _LEASE_FREE_ADMIN_COMMANDS
                    and operation_id
                ):
                    existing = leases.active_for_operation(operation_id)
                    if existing is None:
                        leases.acquire(operation_id, principal)
                        acquired_for_request = True
                    else:
                        # Admin continuations never steal a live actor lease.  An
                        # expired lease must be released explicitly through
                        # recover-lease before the protocol-specific admin action.
                        if leases.is_expired(existing):
                            raise DishRuleError(
                                "CONFLICT",
                                "expired actor lease requires recover-lease first",
                                rule="service_lease_expired",
                                details={"expires_at": existing["expires_at"]},
                            )
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
                    # Admin ownership is request-scoped.  Protocol continuation
                    # never leaves the operation leased to marco-admin.
                    active = leases.active_for_operation(operation_id)
                    if active is not None and leases.is_owned_by(active, principal):
                        leases.release(
                            operation_id,
                            principal,
                            reason=f"admin_command_complete:{command}",
                        )
                        result.setdefault("data", {})["service_lease"] = None
                elif not result.get("ok") and acquired_for_request and operation_id:
                    leases.release(operation_id, principal, reason="admin_command_rejected")
                return result
            except DishRuleError as exc:
                if acquired_for_request and operation_id:
                    try:
                        leases.release(operation_id, principal, reason="admin_command_rejected")
                    except Exception:
                        pass
                task_gid = None
                if operation_id:
                    row = conn.execute(
                        "SELECT task_gid FROM operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    task_gid = None if row is None else row["task_gid"]
                return error_envelope(
                    command, exc, task_gid=task_gid, submission_id=operation_id,
                )
            finally:
                self._close_backend(backend)
                conn.close()

    def create_backup(self, *, label: str = "manual") -> dict[str, Any]:
        with self._maintenance_gate.request():
            try:
                record = self.backup_manager.create(label=label)
                return result_envelope(command="backup-create", data={"backup": record.as_dict()})
            except DishRuleError as exc:
                return error_envelope("backup-create", exc)

    def restore_backup(self, backup_id: str) -> dict[str, Any]:
        with self._maintenance_gate.restore():
            try:
                data = self.backup_manager.restore(backup_id)
                self._restore_fault.clear()
                return result_envelope(command="backup-restore", data=data)
            except DishRuleError as exc:
                if exc.rule == "backup_restore_and_rollback_failed":
                    self._restore_fault.set({
                        "kind": "backup_restore_and_rollback_failed",
                        "rule": exc.rule,
                        "details": dict(exc.details),
                    })
                return error_envelope("backup-restore", exc)
            except Exception as exc:
                self._restore_fault.set({
                    "kind": "backup_restore_recovery_unknown",
                    "error_type": type(exc).__name__,
                })
                error = DishRuleError(
                    "INTERNAL_ERROR",
                    "database restore failed with an unclassified recovery outcome; "
                    "workflow mutations are disabled",
                    rule="backup_restore_recovery_unknown",
                    details={"error_type": type(exc).__name__},
                )
                return error_envelope("backup-restore", error)

    def startup_check(self) -> dict[str, Any]:
        """Report startup state without hiding diagnosis and restore endpoints."""
        repaired = 0
        release = None
        database_initialization_error_type = None
        audit_repair_error_type = None
        compatibility_error_type = None
        try:
            conn = initialize_database(self.config.db_path)
        except Exception as exc:
            database_initialization_error_type = type(exc).__name__
            conn = None
        if conn is not None:
            try:
                repaired = process_command_audit_repairs(conn)
            except Exception as exc:
                audit_repair_error_type = type(exc).__name__
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
