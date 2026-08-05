"""Common process-lifetime exclusion for every mutable database opener."""
from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO

from dish_tool.errors import DishRuleError


class DatabaseProcessLock:
    """Hold one incompatible advisory lock for a governed database lifetime."""

    def __init__(self, path: Path, *, role: str, rule: str = "database_process_lock_held") -> None:
        self.path = Path(path).expanduser()
        self.role = str(role).strip() or "unknown"
        self.rule = rule
        self._handle: IO[str] | None = None

    def acquire(self) -> "DatabaseProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = None
            try:
                handle.seek(0)
                holder = handle.read().strip() or None
            except OSError:
                pass
            handle.close()
            raise DishRuleError(
                "CONFLICT",
                "another dish process already owns the governed database",
                rule=self.rule,
                details={
                    "lock_path": str(self.path),
                    "requested_role": self.role,
                    "recorded_holder": holder,
                },
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"dish-{self.role}\n")
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "DatabaseProcessLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class ServiceProcessLock(DatabaseProcessLock):
    """Compatibility wrapper preserving the service contention rule."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, role="service", rule="service_process_lock_held")
