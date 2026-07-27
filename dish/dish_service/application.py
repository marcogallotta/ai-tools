"""Transport-neutral shared-service boundary around the existing applications."""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_tool.admin import DishAdminApplication
from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.releases import resolve_release
from dish_tool.results import error_envelope, result_envelope

from .config import ServiceConfig
from .leases import LeaseManager, ServicePrincipal

_READ_ONLY_AGENT_COMMANDS = {"sections", "read", "inspect"}
_LEASED_AGENT_COMMANDS = {"prepare", "approve", "reject", "submit"}
_HANDOFF_PHASES = {"await_verification", "held_evidence", "held_human"}


class DishService:
    """Create a fresh database/application boundary for each request.

    SQLite remains the single shared persistent authority. Opening a connection per
    request avoids sharing sqlite connection objects across HTTP worker threads.
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
    def _default_principal(arguments: Mapping[str, Any]) -> ServicePrincipal:
        agent = str(arguments.get("agent") or "local").strip() or "local"
        return ServicePrincipal(owner_id=f"local:{agent}", run_id=f"local:{agent}")

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

    def execute_agent(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
    ) -> dict[str, Any]:
        conn = initialize_database(self.config.db_path)
        acquired_for_request = False
        operation_id = None
        principal = principal or self._default_principal(arguments)
        leases = self._lease_manager(conn)
        try:
            app = DishApplication(
                conn,
                self.backend_factory(),
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
                op = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                if op is not None:
                    if op["status"] in {"completed", "cancelled"}:
                        leases.release_terminal(operation_id, principal)
                    elif op["phase"] in _HANDOFF_PHASES and command in {"prepare", "reject"}:
                        leases.release_for_handoff(
                            operation_id,
                            principal,
                            reason=f"workflow_handoff:{op['phase']}",
                        )
                active = leases.active_for_operation(operation_id)
                result.setdefault("data", {})["service_lease"] = self._lease_payload(active)
            elif not result.get("ok") and acquired_for_request and operation_id:
                # A failed Verification start did not establish actor authority; do
                # not strand the task under the rejected caller's owner lease.
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

    def execute_admin(self, command: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        conn = initialize_database(self.config.db_path)
        try:
            app = DishAdminApplication(
                conn,
                backend=self.backend_factory(),
                release_loader=lambda: self._release(None, include_migrations=True),
            )
            with self._candidate_file(arguments) as prepared:
                return app.execute(command, **prepared)
        except DishRuleError as exc:
            return error_envelope(command, exc)
        finally:
            conn.close()

    def health(self) -> dict[str, Any]:
        conn = initialize_database(self.config.db_path)
        try:
            release = self._release(None)
            return {
                "ok": True,
                "service": "dish",
                "database": {"ok": True, "path": str(self.config.db_path)},
                "compatibility": {
                    "ok": True,
                    "protocol_version": release.protocol_version,
                    "schema_version": release.schema_version,
                },
            }
        except DishRuleError as exc:
            return {
                "ok": False,
                "service": "dish",
                "database": {"ok": False, "path": str(self.config.db_path)},
                "compatibility": {"ok": False, "message": str(exc), "rule": exc.rule},
            }
        finally:
            conn.close()
