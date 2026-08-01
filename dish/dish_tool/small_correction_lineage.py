"""Exact reviewed-to-corrected lineage for Small Verification corrections."""

from __future__ import annotations

import sqlite3
from typing import Mapping, Any

from .content_versions import confirmed_content_version
from .errors import DishRuleError


def assert_small_correction_write_lineage(
    conn: sqlite3.Connection,
    *,
    cycle: Mapping[str, Any],
    corrected_identity: str,
) -> sqlite3.Row:
    """Prove the exact reviewed candidate produced the corrected candidate."""

    corrected_version = confirmed_content_version(
        conn,
        operation_id=str(cycle["operation_id"]),
        task_gid=str(cycle["task_gid"]),
        identity=corrected_identity,
    )
    if corrected_version is None:
        raise DishRuleError(
            "CONFLICT",
            "confirmed content evidence is missing",
            rule="content_version_missing",
            details={"identity": corrected_identity},
        )
    correction_write = conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND purpose='content_write' AND outcome='confirmed'
               AND expected_identity=? AND intended_identity=?
               AND confirmed_content_version_id=?
             ORDER BY started_at DESC, rowid DESC LIMIT 1""",
        (
            cycle["operation_id"],
            cycle["reviewed_identity"],
            corrected_identity,
            corrected_version["content_version_id"],
        ),
    ).fetchone()
    if correction_write is None:
        raise DishRuleError(
            "CONFLICT",
            "Small correction lacks an exact reviewed-to-corrected write binding",
            rule="small_correction_lineage_invalid",
            details={"cycle_id": cycle["cycle_id"]},
        )
    return corrected_version
