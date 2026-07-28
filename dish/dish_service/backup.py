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
from typing import Any

from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database_schema import _validate_current_database, initialize_database
from dish_tool.errors import DishRuleError

_BACKUP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.sqlite3$")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @classmethod
    def _validate_integrity(cls, path: Path) -> int:
        if not path.is_file():
            raise DishRuleError("NOT_FOUND", "backup not found", rule="backup_not_found")
        conn = cls._raw_connection(path)
        try:
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

    def _snapshot_to(self, destination: Path) -> BackupRecord:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = initialize_database(self.db_path)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
            target = self._raw_connection(temp_path)
            try:
                source.backup(target)
            finally:
                target.close()
            self._validate_snapshot(temp_path)
            os.replace(temp_path, destination)
            temp_path = None
            return self._record(destination)
        finally:
            source.close()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def create(self, *, label: str = "manual") -> BackupRecord:
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(label or "manual").strip()).strip("-") or "manual"
        safe_label = safe_label[:32]
        backup_id = f"dish-{_stamp()}-{safe_label}-{uuid.uuid4().hex[:8]}.sqlite3"
        return self._snapshot_to(self._managed_path(backup_id))

    def restore(self, backup_id: str) -> dict[str, Any]:
        source_path = self._managed_path(backup_id)
        source_schema_version = self._validate_integrity(source_path)
        if source_path.resolve() == self.db_path.resolve():
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "the live database cannot be used as its own restore source",
                rule="backup_restore_source_is_live_database",
            )

        pre_restore: BackupRecord | None = None
        pre_restore_error_type: str | None = None
        candidate_path: Path | None = None
        live_replaced = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.db_path.parent,
                prefix=f".{self.db_path.name}.restore.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                candidate_path = Path(handle.name)
            source = self._raw_connection(source_path)
            target = self._raw_connection(candidate_path)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            self._migrate_and_validate_candidate(candidate_path)

            try:
                pre_restore = self.create(label="pre-restore")
            except Exception as exc:
                # Recovery must remain possible when the live database is the
                # invalid object.  The fully prepared candidate is not swapped
                # until after this best-effort validated snapshot attempt.
                pre_restore_error_type = type(exc).__name__

            os.replace(candidate_path, self.db_path)
            live_replaced = True
            candidate_path = None
            for suffix in ("-wal", "-shm"):
                Path(str(self.db_path) + suffix).unlink(missing_ok=True)
            validation = initialize_database(self.db_path)
            validation.close()
        except Exception as restore_exc:
            if pre_restore is None:
                if not live_replaced and isinstance(restore_exc, DishRuleError):
                    raise
                if not live_replaced:
                    raise DishRuleError(
                        "INTERNAL_ERROR",
                        "database restore failed before replacement; the live database was unchanged",
                        rule="backup_restore_failed_live_unchanged",
                        details={
                            "restore_error_type": type(restore_exc).__name__,
                            "database_retained": True,
                        },
                    ) from restore_exc
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
            # Roll back from the automatic pre-restore snapshot. Report whether
            # that rollback was actually proven rather than claiming retention
            # after an unverified second failure.
            rollback_temp: Path | None = None
            try:
                rollback_source = self._raw_connection(self._managed_path(pre_restore.backup_id))
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
                    self._validate_snapshot(rollback_temp)
                    os.replace(rollback_temp, self.db_path)
                    rollback_temp = None
                    for suffix in ("-wal", "-shm"):
                        Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                    validation = initialize_database(self.db_path)
                    validation.close()
                finally:
                    rollback_source.close()
            except Exception as rollback_exc:
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
            finally:
                if rollback_temp is not None:
                    rollback_temp.unlink(missing_ok=True)
            raise DishRuleError(
                "INTERNAL_ERROR",
                "database restore failed; the validated pre-restore database was restored",
                rule="backup_restore_failed_rolled_back",
                details={
                    "restore_error_type": type(restore_exc).__name__,
                    "database_retained": True,
                },
            ) from restore_exc
        finally:
            if candidate_path is not None:
                candidate_path.unlink(missing_ok=True)

        restored = self._record(self.db_path, backup_id=source_path.name)
        return {
            "restored": restored.as_dict(),
            "pre_restore_backup": None if pre_restore is None else pre_restore.as_dict(),
            "pre_restore_unavailable": None if pre_restore is not None else {
                "reason": "live_database_not_validated",
                "error_type": pre_restore_error_type,
            },
            "source_schema_version": source_schema_version,
            "restored_schema_version": SCHEMA_VERSION,
        }
