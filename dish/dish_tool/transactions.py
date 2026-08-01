"""Explicit SQLite transaction ownership primitives.

The repository has three distinct transaction semantics:

* ``immediate_transaction`` owns a writer unit and isolates nested use with a
  savepoint;
* ``join_or_begin_immediate`` joins a caller-owned unit unchanged, or owns one
  when called at the boundary;
* ``read_transaction`` owns a stable deferred-read snapshot and always releases
  it without committing writes;
* ``require_transaction`` documents helpers that may only participate in an
  already active caller transaction.

Keeping these semantics named prevents lower-level persistence helpers from
silently committing a caller's work or inventing a new boundary.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import uuid
from collections.abc import Iterator


_LABEL_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _savepoint_name(label: str) -> str:
    clean = _LABEL_RE.sub("_", str(label).strip()).strip("_") or "transaction"
    return f"dish_{clean}_{uuid.uuid4().hex}"


def _rollback_connection(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


@contextlib.contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Own one stable deferred-read snapshot on a dedicated connection."""

    if conn.in_transaction:
        raise RuntimeError("read_transaction requires an unowned connection")
    conn.execute("BEGIN")
    try:
        yield
    finally:
        _rollback_connection(conn)


@contextlib.contextmanager
def savepoint_transaction(
    conn: sqlite3.Connection, label: str
) -> Iterator[None]:
    """Run one isolated nested unit without owning the outer transaction."""

    savepoint = _savepoint_name(label)
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")


@contextlib.contextmanager
def immediate_transaction(
    conn: sqlite3.Connection, label: str
) -> Iterator[None]:
    """Own a serialized writer unit, using a savepoint when already nested."""

    if conn.in_transaction:
        with savepoint_transaction(conn, label):
            yield
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _rollback_connection(conn)
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            _rollback_connection(conn)
            raise


@contextlib.contextmanager
def join_or_begin_immediate(
    conn: sqlite3.Connection, label: str
) -> Iterator[None]:
    """Join a caller transaction unchanged, or own a new immediate unit.

    This is for helpers whose writes must be atomic with their caller.  An
    exception inside an existing transaction is deliberately left for that
    caller to roll back; the helper must not truncate or commit the caller's
    unit.
    """

    if conn.in_transaction:
        yield
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _rollback_connection(conn)
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            _rollback_connection(conn)
            raise


def require_transaction(conn: sqlite3.Connection, *, operation: str) -> None:
    """Require an active caller-owned transaction for a participating helper."""

    if not conn.in_transaction:
        raise RuntimeError(f"{operation} requires an active caller transaction")
