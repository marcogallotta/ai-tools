"""Restart-safe restore fault marker kept outside the replaceable database."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from dish_tool.models import utc_now


class RestoreFaultMarker:
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path).expanduser()
        self.path = db_path.parent / f"{db_path.name}.restore-fault.json"

    def read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            return {"kind": "restore_fault_marker_unreadable"}
        return payload if isinstance(payload, dict) else {"kind": "restore_fault_marker_invalid"}

    def active(self) -> bool:
        return self.path.exists()

    def set(self, details: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"recorded_at": utc_now(), **dict(details)}
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
            temp_path = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def clear(self) -> None:
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        if existed:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
