from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]+")


def reject_large(
    app: Any,
    tmp_path: Path,
    *,
    operation_id: str,
    agent: str,
    run_id: str,
    candidate_text: str,
):
    """Run the real Large-rejection route and return its command result."""
    inspected = app.execute("inspect", agent=agent, submission_id=operation_id)
    assert inspected["ok"], inspected

    suffix = _SAFE_FILENAME.sub("-", run_id).strip("-") or "run"
    candidate = tmp_path / f"large-rejection-{suffix}.txt"
    candidate.write_text(candidate_text, encoding="utf-8")
    rejected = app.execute(
        "reject",
        agent=agent,
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="method needs replacement",
        file_path=str(candidate),
        run_id=run_id,
    )
    assert rejected["ok"], rejected

    return rejected


def create_large_rejection_successor(
    app: Any,
    tmp_path: Path,
    *,
    operation_id: str,
    agent: str,
    run_id: str,
    candidate_text: str,
):
    """Create the next Verification cycle through the real Large-rejection route."""
    rejected = reject_large(
        app,
        tmp_path,
        operation_id=operation_id,
        agent=agent,
        run_id=run_id,
        candidate_text=candidate_text,
    )
    cycle_id = rejected["data"].get("new_cycle_id")
    assert cycle_id is not None, rejected
    cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id=?",
        (cycle_id,),
    ).fetchone()
    assert cycle is not None
    return cycle
