"""Validated online backup and atomic restore for the shared dish database."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database_schema import _validate_current_database, initialize_database
from dish_tool.errors import DishRuleError

from .restore_plan import RestorePlan

_BACKUP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.sqlite3$")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _immutable_backup_validation_error(
    exc: DishRuleError, *, backup_id: str
) -> DishRuleError:
    """Bind deterministic validation failure to the selected immutable input."""

    details = dict(exc.details)
    details.update({"backup_id": backup_id, "immutable_input": True})
    return DishRuleError(
        exc.code,
        str(exc),
        rule=exc.rule,
        retryable=False,
        details=details,
        errors=exc.errors,
    )


def _backup_destination_error(exc: OSError, *, reason: str) -> DishRuleError:
    """Classify managed-backup filesystem failure without blaming the live DB."""

    return DishRuleError(
        "BACKEND_REJECTED",
        "managed backup destination is unavailable; the live database was not changed",
        rule="backup_destination_unavailable",
        retryable=True,
        details={
            "resource": "managed_backup_directory",
            "reason": reason,
            "error_type": type(exc).__name__,
            "database_retained": True,
        },
    )



@dataclass(frozen=True)
class BackupRecord:
    backup_id: str
    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


class BackupManager:
    """Create validated snapshots and restore only managed snapshot names."""

    def __init__(self, db_path: Path, backup_dir: Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)
        self.backup_dir = Path(backup_dir).expanduser().resolve(strict=False)
        self._restore_checkpoint: Callable[[str, Mapping[str, Any]], None] | None = None

    def set_restore_checkpoint(
        self, callback: Callable[[str, Mapping[str, Any]], None] | None
    ) -> None:
        """Attach the sidecar progress writer used by the service restore route."""
        self._restore_checkpoint = callback

    def _emit_restore_checkpoint(self, stage: str, plan: RestorePlan) -> None:
        if self._restore_checkpoint is not None:
            self._restore_checkpoint(stage, {"plan": plan.as_dict()})

    def _managed_path(self, backup_id: str) -> Path:
        clean = str(backup_id or "").strip()
        if not _BACKUP_ID.fullmatch(clean):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "backup identifier is invalid",
                rule="backup_id_invalid",
            )
        path = self.backup_dir / clean
        if path.parent.resolve() != self.backup_dir:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "backup must be inside the managed backup directory",
                rule="backup_path_outside_managed_directory",
            )
        if path.is_symlink():
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "managed backup filenames must not be symbolic links",
                rule="backup_path_symlink",
            )
        if path.exists() and path.resolve().parent != self.backup_dir:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "backup resolves outside the managed backup directory",
                rule="backup_path_outside_managed_directory",
            )
        return path

    @staticmethod
    def _raw_connection(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            return conn
        except Exception:
            conn.close()
            raise

    @classmethod
    def _validate_integrity(cls, path: Path) -> int:
        if not path.is_file():
            raise DishRuleError("NOT_FOUND", "backup not found", rule="backup_not_found")
        conn: sqlite3.Connection | None = None
        try:
            # Connection setup applies PRAGMAs and can reject corrupt bytes
            # before integrity_check, so it belongs inside this boundary.
            conn = cls._raw_connection(path)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "backup failed SQLite integrity validation",
                    rule="backup_integrity_invalid",
                    details={"integrity": str(integrity)},
                )
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "backup is not a valid dish database",
                rule="backup_database_invalid",
            ) from exc
        finally:
            if conn is not None:
                conn.close()

    @classmethod
    def _validate_snapshot(cls, path: Path) -> None:
        cls._validate_integrity(path)
        conn = cls._raw_connection(path)
        try:
            _validate_current_database(conn)
        finally:
            conn.close()

    @classmethod
    def _migrate_and_validate_candidate(cls, path: Path) -> None:
        """Upgrade a copied restore candidate without mutating the source backup."""
        conn = initialize_database(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        for suffix in ("-wal", "-shm", ".legacy-v2.bak"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        cls._validate_snapshot(path)

    def _record(self, path: Path, *, backup_id: str | None = None) -> BackupRecord:
        return BackupRecord(backup_id or path.name, _sha256(path), path.stat().st_size)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _enforce_owner_only_database_mode(path: Path) -> None:
        """Persist the owner-only mode required for live database installation."""
        os.chmod(path, 0o600)
        file_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)

    def _snapshot_to(self, destination: Path) -> BackupRecord:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not self.backup_dir.is_dir():
                raise NotADirectoryError("managed backup path is not a directory")
        except OSError as exc:
            reason = (
                "permission_denied"
                if isinstance(exc, PermissionError)
                else "not_directory"
                if isinstance(exc, (FileExistsError, NotADirectoryError))
                else "path_unavailable"
            )
            raise _backup_destination_error(exc, reason=reason) from exc

        source = initialize_database(self.db_path)
        temp_path: Path | None = None
        try:
            try:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
            except OSError as exc:
                reason = (
                    "permission_denied"
                    if isinstance(exc, PermissionError)
                    else "not_directory"
                    if isinstance(exc, NotADirectoryError)
                    else "path_unavailable"
                )
                raise _backup_destination_error(exc, reason=reason) from exc
            target = self._raw_connection(temp_path)
            try:
                source.backup(target)
            finally:
                target.close()
            self._validate_snapshot(temp_path)
            try:
                os.replace(temp_path, destination)
                temp_path = None
                self._fsync_directory(destination.parent)
                return self._record(destination)
            except OSError as exc:
                reason = (
                    "permission_denied"
                    if isinstance(exc, PermissionError)
                    else "not_directory"
                    if isinstance(exc, NotADirectoryError)
                    else "path_unavailable"
                )
                raise _backup_destination_error(exc, reason=reason) from exc
        finally:
            source.close()
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def new_backup_id(*, label: str = "manual") -> str:
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(label or "manual").strip()).strip("-") or "manual"
        safe_label = safe_label[:32]
        return f"dish-{_stamp()}-{safe_label}-{uuid.uuid4().hex[:8]}.sqlite3"

    def existing_record(self, backup_id: str) -> BackupRecord | None:
        """Return a validated record for an already-durable managed snapshot."""
        path = self._managed_path(backup_id)
        if not path.exists():
            return None
        try:
            self._validate_snapshot(path)
        except DishRuleError as exc:
            if exc.code == "VALIDATION_FAILED":
                raise _immutable_backup_validation_error(exc, backup_id=path.name) from exc
            raise
        return self._record(path)

    def create(
        self,
        *,
        label: str = "manual",
        backup_id: str | None = None,
    ) -> BackupRecord:
        selected = backup_id or self.new_backup_id(label=label)
        destination = self._managed_path(selected)
        if destination.exists():
            raise DishRuleError(
                "CONFLICT",
                "reserved backup destination already exists",
                rule="backup_destination_exists",
                retryable=False,
                details={"backup_id": selected},
            )
        return self._snapshot_to(destination)

    @staticmethod
    def _file_fingerprint(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink():
            raise DishRuleError(
                "INTERNAL_ERROR",
                "database recovery encountered an invalid filesystem object",
                rule="backup_restore_recovery_filesystem_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        return {"sha256": _sha256(path), "size_bytes": path.stat().st_size}

    def _database_fingerprint(self) -> dict[str, Any]:
        return {
            "main": self._file_fingerprint(self.db_path),
            "wal": self._file_fingerprint(Path(str(self.db_path) + "-wal")),
            "shm": self._file_fingerprint(Path(str(self.db_path) + "-shm")),
        }

    @staticmethod
    def _fingerprints_match(left: Any, right: Any) -> bool:
        return isinstance(left, dict) and isinstance(right, dict) and left == right

    @staticmethod
    def _record_matches(actual: BackupRecord, expected: Mapping[str, Any]) -> bool:
        return (
            actual.sha256 == expected.get("sha256")
            and actual.size_bytes == expected.get("size_bytes")
        )

    def _candidate_path(self, candidate: Mapping[str, Any]) -> Path:
        raw = Path(str(candidate.get("path") or "")).expanduser()
        name = raw.name
        if (
            raw.is_symlink()
            or raw.resolve(strict=False).parent != self.db_path.parent
            or not name.startswith(f".{self.db_path.name}.restore.")
            or not name.endswith(".tmp")
        ):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore recovery checkpoint names an invalid candidate",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        return raw.resolve(strict=False)

    def _verify_candidate(self, plan: Mapping[str, Any]) -> Path:
        candidate = plan.get("candidate")
        if not isinstance(candidate, dict):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore recovery checkpoint has no exact candidate identity",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        path = self._candidate_path(candidate)
        if not path.is_file():
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "prepared restore candidate is missing; recovery cannot prove a safe retry",
                rule="backup_restore_recovery_candidate_missing",
                retryable=False,
                details={"database_retained": False},
            )
        actual = self._record(path, backup_id=str(plan.get("backup_id") or path.name))
        if not self._record_matches(actual, candidate):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "prepared restore candidate no longer matches its durable identity",
                rule="backup_restore_recovery_candidate_mismatch",
                retryable=False,
                details={"database_retained": False},
            )
        return path

    def _assert_source_identity(self, plan: Mapping[str, Any]) -> None:
        source = plan.get("source")
        if not isinstance(source, dict):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore recovery checkpoint has no source identity",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        source_path = self._managed_path(str(plan.get("backup_id") or ""))
        self._validate_integrity(source_path)
        actual = self._record(source_path)
        if not self._record_matches(actual, source):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore source changed after the request began",
                rule="backup_restore_recovery_source_mismatch",
                retryable=False,
                details={"database_retained": False},
            )

    def _cleanup_live_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        self._fsync_directory(self.db_path.parent)

    @staticmethod
    def _plan_from_checkpoint(
        backup_id: str, checkpoint: Mapping[str, Any]
    ) -> tuple[str, RestorePlan]:
        stage = checkpoint.get("stage")
        details = checkpoint.get("details")
        plan = details.get("plan") if isinstance(details, dict) else None
        if not isinstance(stage, str) or not isinstance(plan, Mapping):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore request checkpoint is incomplete",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        if str(plan.get("backup_id") or "") != str(backup_id):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore request checkpoint does not match the selected backup",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        return stage, RestorePlan.from_mapping(plan)

    def _installed_candidate_matches(self, plan: Mapping[str, Any]) -> bool:
        candidate = plan.get("candidate")
        if not isinstance(candidate, dict):
            return False
        main = self._database_fingerprint().get("main")
        return (
            isinstance(main, dict)
            and main.get("sha256") == candidate.get("sha256")
            and main.get("size_bytes") == candidate.get("size_bytes")
        )

    def _result_data(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        installed = plan.get("restored")
        if not isinstance(installed, dict):
            installed = self._record(self.db_path).as_dict()
        return {
            "restored": {
                "source_backup_id": str(plan.get("backup_id") or ""),
                "source_schema_version": plan.get("source_schema_version"),
                "installed_database": {
                    "sha256": installed.get("sha256"),
                    "size_bytes": installed.get("size_bytes"),
                    "schema_version": SCHEMA_VERSION,
                },
            },
            "pre_restore_backup": plan.get("pre_restore_backup"),
            "pre_restore_unavailable": plan.get("pre_restore_unavailable"),
        }

    def _complete_pre_restore_attempt(self, plan: RestorePlan) -> RestorePlan:
        target = plan.get("pre_restore_target")
        if not isinstance(target, dict):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "pre-restore snapshot checkpoint has no exact destination",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": True},
            )
        backup_id = str(target.get("backup_id") or "")
        destination = self._managed_path(backup_id)

        live_at_start = plan.get("live_at_start")
        current = self._database_fingerprint()
        if not self._fingerprints_match(current, live_at_start):
            raise DishRuleError(
                "CONFLICT",
                "live database changed while the restore candidate was prepared",
                rule="backup_restore_recovery_live_changed",
                retryable=False,
                details={"database_retained": True},
            )

        pre_restore: BackupRecord | None = None
        pre_restore_error_type: str | None = None
        try:
            if destination.exists():
                # The destination is chosen and journaled before snapshotting. An
                # existing validated file therefore proves that the interrupted
                # attempt committed; do not create a second pre-restore backup.
                self._validate_snapshot(destination)
                pre_restore = self._record(destination)
            else:
                pre_restore = self._snapshot_to(destination)
        except DishRuleError:
            if destination.exists():
                raise
            pre_restore_error_type = "DishRuleError"
        except Exception as exc:
            pre_restore_error_type = type(exc).__name__

        live_before = self._database_fingerprint()
        if not self._fingerprints_match(live_before, live_at_start):
            raise DishRuleError(
                "CONFLICT",
                "live database changed during the pre-restore snapshot attempt",
                rule="backup_restore_recovery_live_changed",
                retryable=False,
                details={"database_retained": True},
            )
        plan["pre_restore_backup"] = (
            None if pre_restore is None else pre_restore.as_dict()
        )
        plan["pre_restore_unavailable"] = (
            None
            if pre_restore is not None
            else {
                "reason": "live_database_not_validated",
                "error_type": pre_restore_error_type,
            }
        )
        plan["live_before"] = live_before
        self._emit_restore_checkpoint("pre_restore_captured", plan)
        return plan

    def _capture_pre_restore(self, plan: RestorePlan) -> RestorePlan:
        safe_label = "pre-restore"
        backup_id = (
            f"dish-{_stamp()}-{safe_label}-{uuid.uuid4().hex[:8]}.sqlite3"
        )
        plan["pre_restore_target"] = {"backup_id": backup_id}
        # This checkpoint is written before the snapshot side effect. Recovery
        # can inspect the exact destination and either accept it or finish that
        # same attempt without creating a duplicate backup.
        self._emit_restore_checkpoint("pre_restore_attempted", plan)
        return self._complete_pre_restore_attempt(plan)

    def _populate_preparation(self, plan: RestorePlan) -> RestorePlan:
        self._assert_source_identity(plan)
        candidate = plan.get("candidate")
        if not isinstance(candidate, dict):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore preparation checkpoint has no candidate path",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": True},
            )
        candidate_path = self._candidate_path(candidate)
        candidate_path.unlink(missing_ok=True)
        source_path = self._managed_path(str(plan.get("backup_id") or ""))
        source = self._raw_connection(source_path)
        target = self._raw_connection(candidate_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        try:
            self._migrate_and_validate_candidate(candidate_path)
        except DishRuleError as exc:
            if exc.code == "VALIDATION_FAILED":
                raise _immutable_backup_validation_error(
                    exc, backup_id=str(plan.get("backup_id") or "")
                ) from exc
            raise
        self._enforce_owner_only_database_mode(candidate_path)
        candidate_record = self._record(
            candidate_path, backup_id=str(plan.get("backup_id") or candidate_path.name)
        )
        plan["candidate"] = {
            "path": str(candidate_path),
            "sha256": candidate_record.sha256,
            "size_bytes": candidate_record.size_bytes,
        }
        self._emit_restore_checkpoint("candidate_prepared", plan)
        return self._capture_pre_restore(plan)

    def _prepare_restore(self, backup_id: str) -> RestorePlan:
        source_path = self._managed_path(backup_id)
        try:
            source_schema_version = self._validate_integrity(source_path)
        except DishRuleError as exc:
            if exc.code == "VALIDATION_FAILED":
                raise _immutable_backup_validation_error(
                    exc, backup_id=source_path.name
                ) from exc
            raise
        if source_path.resolve() == self.db_path.resolve():
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "the live database cannot be used as its own restore source",
                rule="backup_restore_source_is_live_database",
            )

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path: Path | None = None
        try:
            live_at_start = self._database_fingerprint()
            source_record = self._record(source_path)
            with tempfile.NamedTemporaryFile(
                dir=self.db_path.parent,
                prefix=f".{self.db_path.name}.restore.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                candidate_path = Path(handle.name)
            plan = RestorePlan(
                backup_id=source_path.name,
                source=source_record.as_dict(),
                source_schema_version=source_schema_version,
                restored_schema_version=SCHEMA_VERSION,
                candidate={"path": str(candidate_path)},
                live_at_start=live_at_start,
            )
            self._emit_restore_checkpoint("preparation_started", plan)
            return self._populate_preparation(plan)
        except Exception:
            if candidate_path is not None:
                candidate_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _rolled_back_error(restore_error_type: str) -> DishRuleError:
        return DishRuleError(
            "INTERNAL_ERROR",
            "database restore failed; the validated pre-restore database was restored",
            rule="backup_restore_failed_rolled_back",
            details={
                "restore_error_type": restore_error_type,
                "database_retained": True,
            },
        )

    def _rollback(self, plan: RestorePlan, restore_exc: BaseException) -> None:
        pre_restore = plan.get("pre_restore_backup")
        if not isinstance(pre_restore, dict):
            raise DishRuleError(
                "INTERNAL_ERROR",
                "database restore failed after replacement and no validated pre-restore snapshot was available; "
                "workflow mutations are disabled pending recovery",
                rule="backup_restore_and_rollback_failed",
                details={
                    "restore_error_type": type(restore_exc).__name__,
                    "rollback_error_type": "validated_pre_restore_unavailable",
                    "database_retained": False,
                },
            ) from restore_exc

        rollback_temp: Path | None = None
        try:
            pre_path = self._managed_path(str(pre_restore.get("backup_id") or ""))
            actual_pre = self._record(pre_path)
            if not self._record_matches(actual_pre, pre_restore):
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "validated pre-restore snapshot changed before rollback",
                    rule="backup_restore_and_rollback_failed",
                    details={"database_retained": False},
                )
            rollback_source = self._raw_connection(pre_path)
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.db_path.parent,
                    prefix=f".{self.db_path.name}.rollback.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    rollback_temp = Path(handle.name)
                rollback_target = self._raw_connection(rollback_temp)
                try:
                    rollback_source.backup(rollback_target)
                finally:
                    rollback_target.close()
            finally:
                rollback_source.close()
            self._validate_snapshot(rollback_temp)
            self._enforce_owner_only_database_mode(rollback_temp)
            rollback_record = self._record(
                rollback_temp, backup_id=str(pre_restore.get("backup_id") or rollback_temp.name)
            )
            plan["restore_error_type"] = type(restore_exc).__name__
            plan["rollback_candidate"] = {
                "path": str(rollback_temp),
                "sha256": rollback_record.sha256,
                "size_bytes": rollback_record.size_bytes,
            }
            self._emit_restore_checkpoint("rollback_prepared", plan)
            self._emit_restore_checkpoint("rollback_started", plan)
            self._enforce_owner_only_database_mode(rollback_temp)
            os.replace(rollback_temp, self.db_path)
            rollback_temp = None
            self._fsync_directory(self.db_path.parent)
            self._cleanup_live_sidecars()
            validation = initialize_database(self.db_path)
            validation.close()
            plan["rolled_back"] = self._database_fingerprint()
            self._emit_restore_checkpoint("rolled_back", plan)
        except Exception as rollback_exc:
            if rollback_temp is not None:
                rollback_temp.unlink(missing_ok=True)
            raise DishRuleError(
                "INTERNAL_ERROR",
                "database restore failed and automatic rollback could not be proven; "
                "workflow mutations are disabled pending manual recovery",
                rule="backup_restore_and_rollback_failed",
                details={
                    "restore_error_type": type(restore_exc).__name__,
                    "rollback_error_type": type(rollback_exc).__name__,
                    "database_retained": False,
                },
            ) from rollback_exc
        raise self._rolled_back_error(type(restore_exc).__name__) from restore_exc

    def _validate_installed(self, plan: RestorePlan) -> dict[str, Any]:
        if not self._installed_candidate_matches(plan):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "installed database does not match the prepared restore candidate",
                rule="backup_restore_recovery_installed_mismatch",
                retryable=False,
                details={"database_retained": False},
            )
        self._cleanup_live_sidecars()
        try:
            self._enforce_owner_only_database_mode(self.db_path)
            validation = initialize_database(self.db_path)
            validation.close()
        except Exception as exc:
            self._rollback(plan, exc)
            raise AssertionError("rollback must raise")
        restored = self._record(
            self.db_path, backup_id=str(plan.get("backup_id") or self.db_path.name)
        )
        plan["restored"] = restored.as_dict()
        data = self._result_data(plan)
        plan["result"] = data
        self._emit_restore_checkpoint("validated", plan)
        return data

    def _commit_prepared(
        self, plan: RestorePlan, *, replacement_already_started: bool = False
    ) -> dict[str, Any]:
        candidate_path = self._verify_candidate(plan)
        live_before = plan.get("live_before")
        current = self._database_fingerprint()
        if not self._fingerprints_match(current, live_before):
            if self._installed_candidate_matches(plan):
                return self._validate_installed(plan)
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "live database changed after restore preparation; replacement was not repeated",
                rule="backup_restore_recovery_live_changed",
                retryable=False,
                details={"database_retained": False},
            )
        if not replacement_already_started:
            self._emit_restore_checkpoint("replacement_started", plan)
        try:
            self._enforce_owner_only_database_mode(candidate_path)
            os.replace(candidate_path, self.db_path)
            self._fsync_directory(self.db_path.parent)
            self._cleanup_live_sidecars()
            plan["installed"] = self._record(
                self.db_path, backup_id=str(plan.get("backup_id") or self.db_path.name)
            ).as_dict()
            self._emit_restore_checkpoint("replacement_committed", plan)
        except Exception as exc:
            if self._installed_candidate_matches(plan):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "database replacement may have committed but its durable checkpoint was interrupted",
                    rule="backup_restore_recovery_checkpoint_incomplete",
                    retryable=False,
                    details={
                        "error_type": type(exc).__name__,
                        "database_retained": False,
                    },
                ) from exc
            raise DishRuleError(
                "INTERNAL_ERROR",
                "database restore failed before replacement; the live database was unchanged",
                rule="backup_restore_failed_live_unchanged",
                details={
                    "restore_error_type": type(exc).__name__,
                    "database_retained": True,
                },
            ) from exc
        return self._validate_installed(plan)

    def restore(self, backup_id: str) -> dict[str, Any]:
        plan: RestorePlan | None = None
        try:
            plan = self._prepare_restore(backup_id)
            return self._commit_prepared(plan)
        except Exception as exc:
            installed = bool(plan is not None and self._installed_candidate_matches(plan))
            if plan is not None:
                candidate = plan.get("candidate")
                if isinstance(candidate, dict):
                    try:
                        self._candidate_path(candidate).unlink(missing_ok=True)
                    except DishRuleError:
                        pass
            if isinstance(exc, DishRuleError) and not installed:
                exc.details.setdefault("database_retained", True)
            raise
        # BaseException (including a process-death test sentinel) intentionally
        # leaves a durably identified candidate for restart recovery. SIGKILL
        # skips Python cleanup entirely and has the same filesystem outcome.

    def _resume_rollback(self, plan: RestorePlan, *, already_started: bool) -> None:
        candidate = plan.get("rollback_candidate")
        if not isinstance(candidate, dict):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "rollback recovery checkpoint has no exact candidate identity",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        path = Path(str(candidate.get("path") or "")).expanduser().resolve(strict=False)
        if (
            path.parent != self.db_path.parent
            or not path.name.startswith(f".{self.db_path.name}.rollback.")
            or not path.name.endswith(".tmp")
            or path.is_symlink()
        ):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "rollback recovery checkpoint names an invalid candidate",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={"database_retained": False},
            )
        current_main = self._database_fingerprint().get("main")
        rollback_is_live = (
            isinstance(current_main, dict)
            and current_main.get("sha256") == candidate.get("sha256")
            and current_main.get("size_bytes") == candidate.get("size_bytes")
        )
        if not rollback_is_live:
            if not path.is_file():
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "prepared rollback candidate is missing",
                    rule="backup_restore_recovery_candidate_missing",
                    retryable=False,
                    details={"database_retained": False},
                )
            actual = self._record(path)
            if not self._record_matches(actual, candidate):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "prepared rollback candidate no longer matches its durable identity",
                    rule="backup_restore_recovery_candidate_mismatch",
                    retryable=False,
                    details={"database_retained": False},
                )
            if not already_started:
                self._emit_restore_checkpoint("rollback_started", plan)
            self._enforce_owner_only_database_mode(path)
            os.replace(path, self.db_path)
            self._fsync_directory(self.db_path.parent)
        self._cleanup_live_sidecars()
        self._enforce_owner_only_database_mode(self.db_path)
        validation = initialize_database(self.db_path)
        validation.close()
        plan["rolled_back"] = self._database_fingerprint()
        self._emit_restore_checkpoint("rolled_back", plan)
        raise self._rolled_back_error(str(plan.get("restore_error_type") or "UnknownError"))

    def recover_restore(
        self, backup_id: str, checkpoint: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Resume or reconcile one exact interrupted restore without repeating it blindly."""
        stage = checkpoint.get("stage")
        if stage == "request_accepted":
            details = checkpoint.get("details")
            arguments = details.get("arguments") if isinstance(details, dict) else None
            if not isinstance(arguments, dict) or str(arguments.get("backup_id") or "") != str(backup_id):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "accepted restore request does not match the selected backup",
                    rule="backup_restore_recovery_checkpoint_invalid",
                    retryable=False,
                    details={"database_retained": False},
                )
            return self.restore(backup_id)

        stage, plan = self._plan_from_checkpoint(backup_id, checkpoint)

        if stage == "preparation_started":
            self._assert_source_identity(plan)
            if not self._fingerprints_match(
                self._database_fingerprint(), plan.get("live_at_start")
            ):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "live database changed while restore preparation was interrupted",
                    rule="backup_restore_recovery_live_changed",
                    retryable=False,
                    details={"database_retained": False},
                )
            plan = self._populate_preparation(plan)
            return self._commit_prepared(plan)

        if stage == "candidate_prepared":
            self._assert_source_identity(plan)
            self._verify_candidate(plan)
            if not self._fingerprints_match(
                self._database_fingerprint(), plan.get("live_at_start")
            ):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "live database changed while restore preparation was interrupted",
                    rule="backup_restore_recovery_live_changed",
                    retryable=False,
                    details={"database_retained": False},
                )
            plan = self._capture_pre_restore(plan)
            return self._commit_prepared(plan)

        if stage == "pre_restore_attempted":
            self._assert_source_identity(plan)
            self._verify_candidate(plan)
            plan = self._complete_pre_restore_attempt(plan)
            return self._commit_prepared(plan)

        if stage in {"pre_restore_captured", "replacement_started"}:
            if self._installed_candidate_matches(plan):
                if stage != "replacement_started":
                    self._emit_restore_checkpoint("replacement_started", plan)
                plan["installed"] = self._record(
                    self.db_path, backup_id=str(plan.get("backup_id") or self.db_path.name)
                ).as_dict()
                self._emit_restore_checkpoint("replacement_committed", plan)
                return self._validate_installed(plan)
            return self._commit_prepared(
                plan, replacement_already_started=(stage == "replacement_started")
            )

        if stage == "replacement_committed":
            return self._validate_installed(plan)

        if stage == "validated":
            result = plan.get("result")
            restored = result.get("restored") if isinstance(result, dict) else None
            installed = (
                restored.get("installed_database")
                if isinstance(restored, dict)
                else None
            )
            if not isinstance(installed, dict):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "validated restore checkpoint has no terminal result",
                    rule="backup_restore_recovery_checkpoint_invalid",
                    retryable=False,
                    details={"database_retained": False},
                )
            actual = self._record(self.db_path)
            if not self._record_matches(actual, installed):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "live database no longer matches the validated restore result",
                    rule="backup_restore_recovery_installed_mismatch",
                    retryable=False,
                    details={"database_retained": False},
                )
            self._enforce_owner_only_database_mode(self.db_path)
            self._validate_snapshot(self.db_path)
            return dict(result)

        if stage == "rollback_prepared":
            self._resume_rollback(plan, already_started=False)
            raise AssertionError("rollback recovery must raise")

        if stage == "rollback_started":
            self._resume_rollback(plan, already_started=True)
            raise AssertionError("rollback recovery must raise")

        if stage == "rolled_back":
            expected = plan.get("rolled_back")
            if not self._fingerprints_match(self._database_fingerprint(), expected):
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "live database no longer matches the proven rollback",
                    rule="backup_restore_recovery_rollback_mismatch",
                    retryable=False,
                    details={"database_retained": False},
                )
            self._enforce_owner_only_database_mode(self.db_path)
            self._validate_snapshot(self.db_path)
            raise self._rolled_back_error(str(plan.get("restore_error_type") or "UnknownError"))

        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "restore request has no executable recovery checkpoint",
            rule="backup_restore_recovery_checkpoint_invalid",
            retryable=False,
            details={"stage": stage, "database_retained": False},
        )
