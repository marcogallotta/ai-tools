"""Confirmed content-version lookup shared by workflow domains."""

from __future__ import annotations

import sqlite3


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
