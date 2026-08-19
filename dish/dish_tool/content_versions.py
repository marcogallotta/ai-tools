"""Confirmed content-version lookup shared by workflow domains."""

from __future__ import annotations

import sqlite3

from .database import content_identity as _source_content_identity


# Existing PostgreSQL task-content storage label. Hash semantics remain owned by
# dish_tool.database.content_identity; this constant does not define an algorithm.
CONTENT_IDENTITY_SCHEME = "sha256-title-body-v1"


def content_identity(title: str, body: str) -> str:
    """Return the source-authoritative task-content digest for PostgreSQL callers."""

    return _source_content_identity(title, body).digest


def confirmed_content_version(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    identity: str,
    operation_id: str | None = None,
) -> sqlite3.Row | None:
    """Return the newest confirmed version matching the exact durable identity."""

    clauses = ["task_gid=?", "identity=?", "confirmed=1"]
    parameters: list[str] = [task_gid, identity]
    if operation_id is not None:
        clauses.insert(0, "operation_id=?")
        parameters.insert(0, operation_id)
    return conn.execute(
        f"""SELECT * FROM content_versions
              WHERE {' AND '.join(clauses)}
              ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        tuple(parameters),
    ).fetchone()
