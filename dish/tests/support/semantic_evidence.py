from __future__ import annotations


def insert_operation(conn, operation_id="op", *, status="open", phase="prepare_required"):
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,expected_identity,
               schema_version,created_at,phase,expected_section_gid,
               completed_at,terminal_outcome
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            f"task-{operation_id}",
            "initial",
            status,
            "identity",
            "2",
            "2026-07-28T00:00:00Z",
            phase,
            "research",
            "2026-07-28T00:01:00Z" if status in {"completed", "cancelled"} else None,
            "test" if status in {"completed", "cancelled"} else None,
        ),
    )
