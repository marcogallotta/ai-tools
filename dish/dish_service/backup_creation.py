"""Application orchestration for durable backup creation requests."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope, result_envelope
from dish_tool.transactions import immediate_transaction

from .backup import BackupManager, BackupRecord
from .backup_creation_journal import (
    creation_for_request,
    finish_backup_creation,
    reserve_backup_creation,
    unresolved_backup_creations,
)
from .leases import ServicePrincipal
from .request_replay import (
    begin_request,
    pending_error,
    request_has_uncertain_outcome,
    request_is_unresolved,
    stored_result,
)


class BackupCreationCoordinator:
    """Own backup-create replay, exact-destination recovery, and startup repair."""

    def __init__(
        self,
        *,
        maintenance_gate,
        default_principal: Callable[..., ServicePrincipal],
        initialize_database: Callable[..., sqlite3.Connection],
        backup_manager: Callable[[], BackupManager],
        initialization_error: Callable[[BaseException], DishRuleError],
        preserve_error: Callable[..., DishRuleError],
        execution_unavailable_error: Callable[..., DishRuleError],
        complete_replay: Callable[..., dict[str, Any]],
    ) -> None:
        self._maintenance_gate = maintenance_gate
        self._default_principal = default_principal
        self._initialize_database = initialize_database
        self._backup_manager = backup_manager
        self._initialization_error = initialization_error
        self._preserve_error = preserve_error
        self._execution_unavailable_error = execution_unavailable_error
        self._complete_replay = complete_replay

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
                finish_backup_creation(
                    conn,
                    request_id=request_id,
                    outcome="confirmed",
                    reason="rename_and_directory_fsync_confirmed",
                    record=record,
                )
                authoritative = self._complete_replay(
                    conn, request_id=request_id, result=result
                )
                if not self._backup_result_matches_record(authoritative, record):
                    raise _AuthoritativeReplayWon(authoritative)
                return authoritative
        except _AuthoritativeReplayWon as replay:
            return replay.authoritative

    def _reconcile_backup_creation_destination(
        self,
        conn: sqlite3.Connection,
        *,
        request_id: str,
        creation: Mapping[str, Any],
    ) -> BackupRecord | None:
        """Resolve only the exact reserved destination, never a generated replacement."""
        backup_id = str(creation["backup_id"])
        try:
            record = self._backup_manager().confirm_existing_record(backup_id)
        except DishRuleError as exc:
            try:
                with immediate_transaction(conn, "backup_creation_uncertain"):
                    finish_backup_creation(
                        conn, request_id=request_id, outcome="uncertain",
                        reason=f"reconciliation:{exc.rule}", record=None,
                    )
            except DishRuleError as journal_exc:
                raise journal_exc from exc
            details = dict(exc.details)
            details.update({
                "request_id": request_id,
                "backup_id": backup_id,
                "backup_creation_outcome": "uncertain",
                "exact_reserved_destination_reconciled": True,
            })
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "the exact reserved backup destination could not be reconciled durably",
                rule="backup_creation_reconciliation_uncertain",
                retryable=False,
                details=details,
            ) from exc
        with immediate_transaction(conn, "reconcile_backup_creation"):
            finish_backup_creation(
                conn, request_id=request_id,
                outcome="confirmed" if record is not None else "not_applied",
                reason=(
                    "exact_destination_validated_and_directory_fsynced"
                    if record is not None
                    else "exact_destination_absent"
                ),
                record=record,
            )
        return record

    @staticmethod
    def _backup_not_applied_error(*, request_id: str, backup_id: str) -> DishRuleError:
        return DishRuleError(
            "BACKEND_REJECTED",
            "the reserved backup destination is durably absent",
            rule="backup_creation_not_applied",
            retryable=True,
            details={
                "request_id": request_id,
                "backup_id": backup_id,
                "backup_creation_outcome": "not_applied",
                "exact_reserved_destination_reconciled": True,
            },
        )

    def create(
        self,
        *,
        label: str = "manual",
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self._maintenance_gate.request():
            principal = principal or self._default_principal({}, admin=True)
            replay_started = False
            creation = None
            request_row = None
            try:
                conn = self._initialize_database(
                    surface="admin", command="backup-create",
                    request_id=request_id, principal=principal,
                )
            except Exception as exc:
                return error_envelope(
                    "backup-create", self._initialization_error(exc)
                )
            try:
                manager = self._backup_manager()
                if request_id:
                    request_row, replay_started = begin_request(
                        conn,
                        request_id=request_id, owner_id=principal.owner_id,
                        run_id=principal.run_id, command="backup-create",
                        arguments={"label": label},
                    )
                    creation = creation_for_request(conn, request_id)
                    prior = stored_result(request_row)
                    if prior is not None and bool(prior.get("ok")):
                        return prior
                    if not replay_started and creation is not None:
                        recovered = self._reconcile_backup_creation_destination(
                            conn, request_id=request_id, creation=creation
                        )
                        if recovered is not None:
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
                                conn, request_id=request_id, record=recovered, result=result
                            )
                        if request_has_uncertain_outcome(request_row):
                            result = error_envelope(
                                "backup-create",
                                self._backup_not_applied_error(
                                    request_id=request_id, backup_id=creation["backup_id"]
                                ),
                            )
                            result.setdefault("data", {})["request_id"] = request_id
                            return self._complete_replay(
                                conn, request_id=request_id, result=result
                            )
                        if prior is not None:
                            return prior
                        result = error_envelope(
                            "backup-create",
                            self._backup_not_applied_error(
                                request_id=request_id, backup_id=creation["backup_id"]
                            ),
                        )
                        result.setdefault("data", {})["request_id"] = request_id
                        return self._complete_replay(conn, request_id=request_id, result=result)
                    if prior is not None:
                        return prior
                    if not replay_started:
                        raise pending_error("backup-create", request_id)
                    creation = reserve_backup_creation(
                        conn, request_id=request_id,
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
            except DishRuleError as original_exc:
                exc = original_exc
                if request_id and creation is not None:
                    try:
                        recovered = self._reconcile_backup_creation_destination(
                            conn, request_id=request_id, creation=creation
                        )
                    except DishRuleError as reconciliation_exc:
                        exc = reconciliation_exc
                    else:
                        if recovered is not None:
                            result = result_envelope(
                                command="backup-create",
                                data={
                                    "backup": recovered.as_dict(),
                                    "request_id": request_id,
                                    "backup_recovered_from_ambiguous_completion": True,
                                },
                            )
                            return self._commit_backup_creation_result(
                                conn, request_id=request_id, record=recovered, result=result
                            )
                        if original_exc.code == "BACKEND_UNCERTAIN":
                            exc = self._backup_not_applied_error(
                                request_id=request_id, backup_id=creation["backup_id"]
                            )
                exc = self._preserve_error(
                    exc, execution_occurred=True,
                    request_id_consumed=bool(request_id and replay_started),
                )
                result = error_envelope("backup-create", exc)
                if request_id and replay_started:
                    result.setdefault("data", {})["request_id"] = request_id
                    self._complete_replay(conn, request_id=request_id, result=result)
                return result
            except Exception as exc:
                result = error_envelope(
                    "backup-create",
                    self._execution_unavailable_error(
                        exc, request_id_consumed=bool(request_id and replay_started),
                    ),
                )
                if request_id:
                    result.setdefault("data", {})["request_id"] = request_id
                return result
            finally:
                conn.close()

    def reconcile_startup(
        self, conn: sqlite3.Connection
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "discovered": 0,
            "confirmed": 0,
            "not_applied": 0,
            "uncertain": 0,
            "errors": [],
        }
        for creation in unresolved_backup_creations(conn):
            summary["discovered"] += 1
            request_id = str(creation["request_id"])
            try:
                record = self._reconcile_backup_creation_destination(
                    conn, request_id=request_id, creation=creation
                )
                request = conn.execute(
                    "SELECT * FROM service_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if record is not None:
                    result = result_envelope(
                        command="backup-create",
                        data={
                            "backup": record.as_dict(),
                            "request_id": request_id,
                            "request_replayed": True,
                            "backup_recovered_during_startup": True,
                        },
                    )
                    self._commit_backup_creation_result(
                        conn, request_id=request_id, record=record, result=result
                    )
                    summary["confirmed"] += 1
                else:
                    if request is not None and request_is_unresolved(request):
                        result = error_envelope(
                            "backup-create",
                            self._backup_not_applied_error(
                                request_id=request_id, backup_id=creation["backup_id"]
                            ),
                        )
                        result.setdefault("data", {})["request_id"] = request_id
                        self._complete_replay(conn, request_id=request_id, result=result)
                    summary["not_applied"] += 1
            except DishRuleError as exc:
                summary["uncertain"] += 1
                summary["errors"].append({
                    "request_id": request_id,
                    "backup_id": creation["backup_id"],
                    "rule": exc.rule,
                    "error_type": type(exc).__name__,
                })
        return summary

