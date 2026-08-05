"""Persistent marker separating service-owned and local development databases."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from dish_tool.errors import DishRuleError
from dish_tool.models import utc_now




def canonical_database_path(db_path: Path) -> Path:
    """Return the filesystem identity used for service ownership artifacts."""
    return Path(db_path).expanduser().resolve(strict=False)


def database_process_lock_path(db_path: Path) -> Path:
    canonical = canonical_database_path(db_path)
    return canonical.with_suffix(canonical.suffix + ".service.lock")


def service_process_lock_path(db_path: Path) -> Path:
    """Compatibility alias for the shared database process lock path."""
    return database_process_lock_path(db_path)


class ServiceDatabaseOwnership:
    """Mark a database as belonging exclusively to the shared service runtime."""

    def __init__(self, db_path: Path) -> None:
        db_path = canonical_database_path(db_path)
        self.db_path = db_path
        self.path = db_path.parent / f"{db_path.name}.service-owned.json"

    def mark(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "dish-service-owned-database",
            "database": self.db_path.name,
            "marked_at": utc_now(),
        }
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def assert_local_access_allowed(self) -> None:
        if self.path.exists():
            raise DishRuleError(
                "PROTOCOL_INCOMPATIBLE",
                "this database is owned by dish-service and cannot be opened in direct local mode",
                rule="service_owned_database",
                details={"database": str(self.db_path), "marker": str(self.path)},
            )
