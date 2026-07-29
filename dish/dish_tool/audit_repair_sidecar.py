"""Crash-safe coordination for emergency invocation-audit repair sidecars."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


@dataclass(frozen=True)
class AuditRepairSidecarPaths:
    main: pathlib.Path
    claim: pathlib.Path
    lock: pathlib.Path


def _database_path(conn: sqlite3.Connection) -> pathlib.Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    db_path = "" if row is None else str(row[2] or "")
    if not db_path or db_path == ":memory:":
        return None
    return pathlib.Path(db_path)


def _paths(conn: sqlite3.Connection) -> AuditRepairSidecarPaths | None:
    database = _database_path(conn)
    if database is None:
        return None
    base = pathlib.Path(str(database) + ".audit-repair.jsonl")
    return AuditRepairSidecarPaths(
        main=base,
        claim=pathlib.Path(str(base) + ".importing"),
        lock=pathlib.Path(str(base) + ".lock"),
    )


def fsync_parent(path: pathlib.Path) -> None:
    """Make a sidecar rename/unlink durable when the platform supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def locked_audit_repair_sidecar(
    conn: sqlite3.Connection,
) -> Iterator[AuditRepairSidecarPaths | None]:
    """Serialize writers and importers for the database's emergency sidecar."""
    paths = _paths(conn)
    if paths is None:
        yield None
        return
    paths.main.parent.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield paths
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_audit_repair(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
) -> bool:
    """Append and fsync an emergency repair without racing an importer."""
    with locked_audit_repair_sidecar(conn) as paths:
        if paths is None:
            return False
        encoded = json.dumps(dict(payload), sort_keys=True) + "\n"
        existed = paths.main.exists()
        with paths.main.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            fsync_parent(paths.main)
        return True
