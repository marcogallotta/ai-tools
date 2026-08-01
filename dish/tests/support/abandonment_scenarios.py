from __future__ import annotations

import sqlite3

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.abandonment import settle_abandonment_frontier
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import (
    complete_operation_step,
    confirm_task_content,
    create_abandonment_attempt_in_transaction,
    create_operation,
    create_verification_cycle,
    declare_operation_step,
)
from dish_tool.models import OperationActors
from tests.support.abandonment import Backend, _abandon, _source


PLANNING_NOTES = """### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
"""


class FrontierBackend:
    def __init__(self, *, title: str, notes: str, section: str):
        self.title = title
        self.notes = notes
        self.section = section
        self.sections = [
            {"gid": "pi", "name": "Planning (Incomplete)"},
            {"gid": "rq", "name": "Research Queue"},
            {"gid": "vq", "name": "Verification Queue"},
            {"gid": "12345", "name": "Sichuan"},
            {"gid": "src", "name": "Sourcing"},
            {"gid": "ref", "name": "Reference"},
        ]

    def list_sections(self, project_gid):
        assert project_gid == COOKING_PROJECT_GID
        return self.sections

    def read_task(self, gid):
        return {
            "gid": gid,
            "name": self.title,
            "notes": self.notes,
            "completed": False,
            "modified_at": "now",
            "projects": [{"gid": COOKING_PROJECT_GID}],
            "memberships": [
                {
                    "project": {"gid": COOKING_PROJECT_GID},
                    "section": {"gid": self.section},
                }
            ],
        }

    def update_task_content(self, *, task_gid, title, notes):
        raise AssertionError("Stage 4 must not perform an external content write")

    def move_task_to_section(self, *, task_gid, section_gid):
        raise AssertionError("Stage 4 must not perform an external movement")


def count_rows(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def abandonment_in_state(conn, backend: Backend, *, target_status: str) -> sqlite3.Row:
    """Drive a fresh abandonment on task "task" to exactly target_status."""
    if target_status == "blocked_manual_reconciliation":
        source = _source(conn, backend, kind="initial")
        declare_operation_step(
            conn,
            source["operation_id"],
            "candidate_write",
            {"title": "Changed", "notes": "changed", "schema_version": "2"},
        )
        _abandon(conn, source)
        settle_abandonment_frontier(
            conn, backend, abandonment_id="abandonment", reason="gone"
        )
    elif target_status == "awaiting_hold_resolution":
        source = _source(conn, backend, kind="initial", phase="held_evidence")
        declare_operation_step(
            conn,
            source["operation_id"],
            "research_preconstruction_hold",
            {
                "route": "evidence",
                "resume_status": "pending-research",
                "candidate_content_existed": False,
            },
        )
        complete_operation_step(
            conn, source["operation_id"], "research_preconstruction_hold"
        )
        _abandon(conn, source)
        settle_abandonment_frontier(
            conn, backend, abandonment_id="abandonment", reason="gone"
        )
    elif target_status == "awaiting_successor_claim":
        source = _source(conn, backend, kind="planning")
        _abandon(conn, source)
        settle_abandonment_frontier(
            conn, backend, abandonment_id="abandonment", reason="gone"
        )
    else:
        raise ValueError(target_status)

    stored_status = conn.execute(
        "SELECT status FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[0]
    assert stored_status == target_status
    return source


def frontier_operation(
    conn: sqlite3.Connection,
    backend: FrontierBackend,
    *,
    kind: str,
    phase: str = "prepare_required",
    run_id: str = "dead-run",
):
    baseline = confirm_task_content(
        conn,
        task_gid="task",
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
    )
    row = create_operation(
        conn,
        task_gid="task",
        operation_kind=kind,
        expected_identity=baseline.digest,
        schema_version="2",
        expected_section_gid=backend.section,
        actors=OperationActors(
            editor_agent="gpt",
            researcher_agent="gpt",
            run_id=run_id,
        ),
    )
    if phase != "prepare_required":
        conn.execute(
            "UPDATE operations SET phase=? WHERE operation_id=?",
            (phase, row["operation_id"]),
        )
    return conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (row["operation_id"],)
    ).fetchone()


def frontier_abandonment(
    conn: sqlite3.Connection,
    operation: sqlite3.Row,
    *,
    cycle: sqlite3.Row | None = None,
    abandonment_id: str = "abandonment",
):
    lease = LeaseManager(conn).acquire(
        operation["operation_id"],
        ServicePrincipal("owner", "dead-run"),
        context_cycle_id=None if cycle is None else cycle["cycle_id"],
    )
    LeaseManager(conn).release(
        operation["operation_id"], None, reason="stale actor released", admin=True
    )
    conn.execute("BEGIN IMMEDIATE")
    row = create_abandonment_attempt_in_transaction(
        conn,
        abandonment_id=abandonment_id,
        task_gid=operation["task_gid"],
        source_operation_id=operation["operation_id"],
        source_lease_id=lease["lease_id"],
        abandoned_owner_id="owner",
        abandoned_run_id="dead-run",
        attempt_cycle_id=None if cycle is None else cycle["cycle_id"],
        reason="chat permanently unavailable",
    )
    conn.execute("COMMIT")
    return row


def persistence_source(
    conn: sqlite3.Connection,
    *,
    task_gid: str = "task-1",
    operation_kind: str = "initial",
    owner_id: str = "owner-1",
    run_id: str = "run-1",
    phase: str = "prepare_required",
    cycle: bool = False,
):
    identity = confirm_task_content(
        conn,
        task_gid=task_gid,
        title=f"Dish {task_gid}",
        notes=f"Notes {task_gid}",
        schema_version="2",
    )
    operation = create_operation(
        conn,
        task_gid=task_gid,
        operation_kind=operation_kind,
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="section-1",
        actors=OperationActors(
            editor_agent="gpt",
            researcher_agent="gpt",
            run_id=run_id,
        ),
    )
    if phase != "prepare_required":
        conn.execute(
            "UPDATE operations SET phase=? WHERE operation_id=?",
            (phase, operation["operation_id"]),
        )
    cycle_row = None
    context_cycle_id = None
    if cycle:
        cycle_row = create_verification_cycle(
            conn,
            operation_id=operation["operation_id"],
            task_gid=task_gid,
            cycle_number=1,
            protocol_release="verification-v1",
            protocol_text="protocol",
            verifier_agent="claude",
            run_id=run_id,
            independence_attestation="independent",
        )
        context_cycle_id = cycle_row["cycle_id"]
    lease = LeaseManager(conn).acquire(
        operation["operation_id"],
        ServicePrincipal(owner_id, run_id),
        context_cycle_id=context_cycle_id,
    )
    source_version_id = conn.execute(
        "SELECT last_confirmed_content_version_id FROM task_content_state WHERE task_gid=?",
        (task_gid,),
    ).fetchone()[0]
    return operation, lease, cycle_row, source_version_id


def start_abandonment(
    conn: sqlite3.Connection,
    *,
    operation,
    lease,
    cycle=None,
    abandonment_id: str = "abandonment-1",
):
    if lease["released_at"] is None:
        LeaseManager(conn).release(
            operation["operation_id"], None, reason="stale actor released", admin=True
        )
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
        ).fetchone()
    conn.execute("BEGIN IMMEDIATE")
    row = create_abandonment_attempt_in_transaction(
        conn,
        abandonment_id=abandonment_id,
        task_gid=operation["task_gid"],
        source_operation_id=operation["operation_id"],
        source_lease_id=lease["lease_id"],
        abandoned_owner_id=lease["owner_id"],
        abandoned_run_id=lease["run_id"],
        attempt_cycle_id=None if cycle is None else cycle["cycle_id"],
        reason="conversation permanently unavailable",
    )
    conn.execute("COMMIT")
    return row
