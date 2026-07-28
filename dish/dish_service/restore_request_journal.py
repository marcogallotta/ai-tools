"""Restore-safe durable request identity kept outside the replaceable database."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator, Mapping

from dish_tool.errors import DishRuleError
from dish_tool.models import utc_now

from .request_replay import request_hash


class RestoreRequestJournal:
    """Atomic per-request journal for database restore mutations.

    The ordinary request ledger lives inside the database and therefore cannot
    protect the mutation that replaces that database.  Restore requests use a
    sibling sidecar directory, guarded by an advisory file lock and atomically
    replaced JSON records.
    """

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path).expanduser()
        self.directory = db_path.parent / f"{db_path.name}.restore-requests"
        self.lock_path = self.directory / ".lock"

    def _record_path(self, request_id: str) -> Path:
        return self.directory / f"{request_id}.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "restore request journal is unreadable; do not repeat the restore",
                rule="restore_request_journal_unreadable",
                retryable=False,
                details={"path": str(path)},
            ) from exc
        if not isinstance(value, dict):
            raise DishRuleError(
                "INTERNAL_ERROR",
                "restore request journal is invalid; do not repeat the restore",
                rule="restore_request_journal_invalid",
                retryable=False,
                details={"path": str(path)},
            )
        return value

    @staticmethod
    def _write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(dict(payload), handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def begin(
        self,
        *,
        request_id: str,
        owner_id: str,
        run_id: str,
        command: str,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        digest = request_hash(command, arguments)
        path = self._record_path(request_id)
        with self._locked():
            row = self._read(path)
            if row is None:
                row = {
                    "request_id": request_id,
                    "owner_id": owner_id,
                    "run_id": run_id,
                    "command": command,
                    "request_hash": digest,
                    "arguments": dict(arguments),
                    "status": "pending",
                    "result": None,
                    "created_at": utc_now(),
                    "completed_at": None,
                }
                self._write(path, row)
                return row, True
            if (
                row.get("owner_id") != owner_id
                or row.get("run_id") != run_id
                or row.get("command") != command
                or row.get("request_hash") != digest
            ):
                raise DishRuleError(
                    "CONFLICT",
                    "request ID was already used for different work",
                    rule="service_request_identity_conflict",
                    details={"request_id": request_id},
                )
            return row, False

    @staticmethod
    def stored_result(row: Mapping[str, Any]) -> dict[str, Any] | None:
        if row.get("status") not in {"completed", "uncertain"}:
            return None
        stored = row.get("result")
        if not isinstance(stored, dict):
            return None
        result = json.loads(json.dumps(stored))
        result.setdefault("data", {})["request_replayed"] = True
        result["data"]["request_id"] = row.get("request_id")
        return result

    def complete(self, *, request_id: str, result: Mapping[str, Any]) -> None:
        path = self._record_path(request_id)
        with self._locked():
            row = self._read(path)
            if row is None:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "restore request journal entry is missing",
                    rule="restore_request_journal_missing",
                    retryable=False,
                    details={"request_id": request_id},
                )
            if row.get("status") != "pending":
                return
            row["status"] = (
                "uncertain" if result.get("code") == "BACKEND_UNCERTAIN" else "completed"
            )
            row["result"] = dict(result)
            row["completed_at"] = utc_now()
            self._write(path, row)
