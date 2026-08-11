from __future__ import annotations


"""Shared helpers extracted from test_abandonment_stage_successors.py."""


import sqlite3

import uuid

from pathlib import Path

import pytest

from dish_service.application import DishService

from dish_service.command_spec import validate_action_request

from dish_service.config import ServiceConfig

from dish_service.leases import LeaseManager, ServicePrincipal

from dish_tool.abandonment import settle_abandonment_frontier

from dish_tool.application_service import CurrentWorkflowService

from dish_tool.constants import COOKING_PROJECT_GID

from dish_tool.database import (
    complete_operation_step,
    confirm_task_content,
    create_abandonment_attempt_in_transaction,
    create_operation,
    declare_operation_step,
)

from dish_tool.database_initialization import initialize_database

from dish_tool.errors import DishRuleError

from dish_tool.models import OperationActors, ResolvedRelease

from dish_tool.step5 import claim_prepared_stage_successor

from dish_tool.step8 import resolve_hold

from dish_tool.task_store import LiveTask

from tests.support.planning_intent import confirmed_planning_start
from tests.support.asana_backend import StatefulAsanaBackend

_NUMERIC_TASK_GID = "1234567890123456"


def _numeric_task_source(
    conn: sqlite3.Connection,
    backend: "Backend",
    *,
    task_gid: str = _NUMERIC_TASK_GID,
):
    baseline = confirm_task_content(
        conn, task_gid=task_gid, title=backend.title, notes=backend.notes,
        schema_version="2", boundary="test-baseline",
    )
    actors = OperationActors(editor_agent="gpt", researcher_agent=None, run_id="dead-run")
    return create_operation(
        conn, task_gid=task_gid, operation_kind="planning",
        expected_identity=baseline.digest, schema_version="2",
        expected_section_gid=backend.section, actors=actors,
    )


class Backend(StatefulAsanaBackend):
    def __init__(
        self,
        *,
        title: str = "Bare",
        notes: str = "",
        section: str = "rq",
        task_gid: str = "task",
    ):
        super().__init__(
            title=title,
            notes=notes,
            section=section,
            task_gid=task_gid,
        )
        self.forbid(
            "update_task_content",
            "clean stage successor creation must not write Asana",
        )
        self.forbid(
            "move_task_to_section",
            "clean stage successor creation must not move Asana",
        )


def _release(role: str) -> ResolvedRelease:
    return ResolvedRelease(
        version="test-release",
        commit="test",
        root=Path("."),
        protocols={role: f"{role} protocol"},
        schema_version="2",
        schema={},
        schema_text="{}",
        requested_protocol_role=role,
    )

def _source(
    conn: sqlite3.Connection,
    backend: Backend,
    *,
    kind: str,
    phase: str = "prepare_required",
    run_id: str = "dead-run",
    initial_steps=None,
):
    baseline = confirm_task_content(
        conn,
        task_gid="task",
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
        boundary="test-baseline",
    )
    actors = OperationActors(
        editor_agent="gpt" if kind in {"planning", "change"} else None,
        researcher_agent="gpt" if kind == "initial" else None,
        run_id=run_id,
    )
    operation = create_operation(
        conn,
        task_gid="task",
        operation_kind=kind,
        expected_identity=baseline.digest,
        schema_version="2",
        expected_section_gid=backend.section,
        actors=actors,
        initial_steps=initial_steps,
    )
    if phase != "prepare_required":
        conn.execute(
            "UPDATE operations SET phase=? WHERE operation_id=?",
            (phase, operation["operation_id"]),
        )
    return conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)
    ).fetchone()

def _abandon(conn: sqlite3.Connection, operation: sqlite3.Row, *, abandonment_id="abandonment"):
    lease = LeaseManager(conn).acquire(
        operation["operation_id"], ServicePrincipal("owner", "dead-run")
    )
    LeaseManager(conn).release(
        operation["operation_id"], None, reason="stale actor released", admin=True
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = create_abandonment_attempt_in_transaction(
            conn,
            abandonment_id=abandonment_id,
            task_gid=operation["task_gid"],
            source_operation_id=operation["operation_id"],
            source_lease_id=lease["lease_id"],
            abandoned_owner_id="owner",
            abandoned_run_id="dead-run",
            reason="conversation permanently unavailable",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return row

def _live(backend: Backend) -> LiveTask:
    return LiveTask(
        gid="task",
        title=backend.title,
        notes=backend.notes,
        section_gid=backend.section,
        completed=False,
        modified_at="now",
    )


class RepairBackend(Backend):
    """Backend that permits reconciliation to repair task content and placement."""

    def update_task_content(self, *, task_gid, title, notes):
        assert task_gid == self.task_gid
        self.title = title
        self.notes = notes

    def move_task_to_section(self, *, task_gid, section_gid):
        assert task_gid == self.task_gid
        self.section = section_gid
