"""Single-process ownership guard for the laptop-hosted service."""
from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO

from dish_tool.errors import DishRuleError


class ServiceProcessLock:
    """Hold an advisory host lock for the lifetime of one service process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._handle: IO[str] | None = None

    def acquire(self) -> "ServiceProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise DishRuleError(
                "CONFLICT",
                "another dish service process already owns the shared database",
                rule="service_process_lock_held",
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write("dish-service\n")
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

    def __enter__(self) -> "ServiceProcessLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
