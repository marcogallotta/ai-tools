from __future__ import annotations

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
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors, ResolvedRelease
from dish_tool.step5 import claim_prepared_stage_successor
from dish_tool.step8 import resolve_hold
from dish_tool.task_store import LiveTask

from tests.planning_intent_support import confirmed_planning_start
from tests.support.abandonment import (
    Backend,
    _abandon,
    _live,
    _release,
    _source,

)
from tests.support.verification import TASK












def test_clean_planning_abandonment_creates_exact_unowned_successor():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    _abandon(conn, source)

    result = CurrentWorkflowService(conn, backend).settle_abandonment_frontier(
        "abandonment", reason="conversation permanently unavailable"
    )

    successor_id = result["successor_operation_id"]
    assert result["required_action"] == {
        "surface": "connected-agent",
        "command": "start",
        "arguments": {
            "task_gid": "task",
            "kind": "planning",
            "prepared_operation_id": successor_id,
        },
    }
    source_after = conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (source["operation_id"],),
    ).fetchone()
    assert tuple(source_after) == ("cancelled", "terminal", "agent_abandoned")
    successor = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (successor_id,)
    ).fetchone()
    assert successor["status"] == "open"
    assert successor["phase"] == "prepare_required"
    assert successor["successor_claim_mode"] == "stage_actor"
    assert successor["editor_agent"] is None
    assert successor["run_id"] is None
    assert conn.execute(
        "SELECT status,outcome FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[:] == ("awaiting_successor_claim", "restart_prepared")


def test_prepared_planning_claim_rejects_abandoned_run_then_binds_fresh_run():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    _abandon(conn, source)
    result = settle_abandonment_frontier(
        conn, backend, abandonment_id="abandonment", reason="gone"
    )
    successor_id = result["successor_operation_id"]

    with pytest.raises(DishRuleError) as raised:
        claim_prepared_stage_successor(
            conn,
            live=_live(backend),
            release=_release("planning"),
            kind="planning",
            agent="gpt",
            run_id="dead-run",
            prepared_operation_id=successor_id,
        )
    assert raised.value.rule == "abandoned_run_claim_forbidden"

    claimed = claim_prepared_stage_successor(
        conn,
        live=_live(backend),
        release=_release("planning"),
        kind="planning",
        agent="gpt",
        run_id="fresh-run",
        prepared_operation_id=successor_id,
    )
    assert claimed["editor_agent"] == "gpt"
    assert claimed["run_id"] == "fresh-run"
    assert claimed["successor_claim_mode"] == "none"
    assert conn.execute(
        "SELECT role,run_id FROM operation_actor_facts WHERE operation_id=?",
        (successor_id,),
    ).fetchone()[:] == ("planner", "fresh-run")
    assert conn.execute(
        "SELECT status,outcome FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[:] == ("completed", "restarted")


def test_clean_change_successor_preserves_exact_completed_change_intent():
    conn = initialize_database(":memory:")
    backend = Backend(section="rq")
    intent = {"level": "small", "reason": "Correct salt"}
    source = _source(
        conn,
        backend,
        kind="change",
        initial_steps={"change_intent": intent},
    )
    _abandon(conn, source)
    result = settle_abandonment_frontier(
        conn, backend, abandonment_id="abandonment", reason="gone"
    )
    successor_id = result["successor_operation_id"]
    step = conn.execute(
        "SELECT intended_json,completed_at FROM operation_steps WHERE operation_id=? AND step_name='change_intent'",
        (successor_id,),
    ).fetchone()
    assert step is not None and step["completed_at"] is not None

    with pytest.raises(DishRuleError) as raised:
        claim_prepared_stage_successor(
            conn,
            live=_live(backend),
            release=_release("research"),
            kind="change",
            agent="gpt",
            run_id="fresh-run",
            prepared_operation_id=successor_id,
            change_level="large",
            change_reason="Different intent",
        )
    assert raised.value.rule == "prepared_successor_change_intent_mismatch"

    claimed = claim_prepared_stage_successor(
        conn,
        live=_live(backend),
        release=_release("research"),
        kind="change",
        agent="gpt",
        run_id="fresh-run",
        prepared_operation_id=successor_id,
        change_level="small",
        change_reason="Correct salt",
    )
    assert claimed["editor_agent"] == "gpt"
    assert claimed["run_id"] == "fresh-run"


def test_resolving_abandoned_preconstruction_hold_creates_research_successor():
    conn = initialize_database(":memory:")
    backend = Backend(section="rq")
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
    held = settle_abandonment_frontier(
        conn, backend, abandonment_id="abandonment", reason="gone"
    )
    assert held["classification"]["outcome"] == "awaiting_hold_resolution"

    resolved = resolve_hold(
        conn,
        backend,
        operation_id=source["operation_id"],
        resolution_kind="evidence",
        detail="Evidence supplied",
        resume_status="pending-research",
        honest_root=Path("."),
    )

    successor_id = resolved["operation_id"]
    assert successor_id != source["operation_id"]
    assert resolved["required_action"]["arguments"]["prepared_operation_id"] == successor_id
    assert conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (source["operation_id"],),
    ).fetchone()[:] == ("cancelled", "terminal", "agent_abandoned")
    successor = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (successor_id,)
    ).fetchone()
    assert successor["operation_kind"] == "initial"
    assert successor["phase"] == "prepare_required"
    assert successor["researcher_agent"] is None
    assert successor["run_id"] is None
    assert successor["successor_claim_mode"] == "stage_actor"
    assert conn.execute(
        "SELECT status FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[0] == "awaiting_successor_claim"



def test_real_reject_route_hold_then_abandonment_creates_research_successor(tmp_path):
    """Producer-contract companion to test_resolving_abandoned_preconstruction_hold_creates_research_successor.

    Reaches the held_evidence phase through the real "start" + "reject" command
    path (dish_tool.step8.reject_route -> _preconstruction_research_hold)
    instead of hand-declaring the research_preconstruction_hold step, proving
    the real producer leaves the shape the fabricated-state test assumes.
    """
    from dish_tool.commands import DishApplication

    lines = TASK.splitlines()
    backend = Backend(
        title=lines[0], notes="\n".join(lines[1:]) + "\n", section="rq"
    )
    honest = tmp_path / "honest"
    honest.mkdir()

    def release(role=None):
        return _release(role or "research")

    app = DishApplication(
        initialize_database(tmp_path / "dish.db"), backend, release_loader=release
    )
    started = app.execute(
        "start", agent="gpt", task_gid="task", kind="initial",
        change_level=None, change_reason=None, run_id="constructor-run",
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    held = app.execute(
        "reject", agent="gpt", submission_id=operation_id, route="evidence",
        reason="need supplier confirmation before construction",
        resume_status="pending-research", run_id="constructor-run",
    )
    assert held["ok"]
    assert app.conn.execute(
        "SELECT phase FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == "held_evidence"

    source = app.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    _abandon(app.conn, source)
    settled = settle_abandonment_frontier(
        app.conn, backend, abandonment_id="abandonment", reason="gone"
    )
    assert settled["classification"]["outcome"] == "awaiting_hold_resolution"

    resolved = resolve_hold(
        app.conn,
        backend,
        operation_id=operation_id,
        resolution_kind="evidence",
        detail="Evidence supplied",
        resume_status="pending-research",
        honest_root=honest,
    )

    successor_id = resolved["operation_id"]
    assert successor_id != operation_id
    assert resolved["required_action"]["arguments"]["prepared_operation_id"] == successor_id
    assert app.conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[:] == ("cancelled", "terminal", "agent_abandoned")
    successor = app.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (successor_id,)
    ).fetchone()
    assert successor["operation_kind"] == "initial"
    assert successor["phase"] == "prepare_required"
    assert successor["successor_claim_mode"] == "stage_actor"
    assert app.conn.execute(
        "SELECT status FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[0] == "awaiting_successor_claim"


def test_start_without_prepared_id_auto_claims_ready_successor(tmp_path):
    """A plain `start` (no prepared_operation_id) auto-claims a ready successor.

    Companion to test_prepared_planning_claim_rejects_abandoned_run_then_binds_fresh_run,
    proving the caller no longer needs to echo the exact prepared_operation_id
    back: dish resolves it from task_gid alone once the successor is ready.
    """
    from dish_tool.commands import DishApplication

    lines = TASK.splitlines()
    backend = Backend(
        title=lines[0], notes="\n".join(lines[1:]) + "\n", section="rq"
    )

    def release(role=None):
        return _release(role or "research")

    app = DishApplication(
        initialize_database(":memory:"), backend, release_loader=release
    )
    conn = app.conn
    started = app.execute(
        "start", agent="gpt", task_gid="task", kind="initial",
        change_level=None, change_reason=None, run_id="dead-run",
    )
    assert started["ok"]
    source = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (started["submission_id"],)
    ).fetchone()
    _abandon(conn, source)
    settled = settle_abandonment_frontier(
        conn, backend, abandonment_id="abandonment", reason="gone"
    )
    successor_id = settled["successor_operation_id"]

    old_run = app.execute(
        "start", agent="gpt", task_gid="task", kind="initial",
        change_level=None, change_reason=None, run_id="dead-run",
    )
    assert old_run["errors"][0]["rule"] == "abandoned_run_claim_forbidden"

    claimed = app.execute(
        "start", agent="gpt", task_gid="task", kind="initial",
        change_level=None, change_reason=None, run_id="fresh-run",
    )
    assert claimed["ok"]
    assert claimed["submission_id"] == successor_id
    assert conn.execute(
        "SELECT successor_claim_mode,run_id FROM operations WHERE operation_id=?",
        (successor_id,),
    ).fetchone()[:] == ("none", "fresh-run")
    assert conn.execute(
        "SELECT status,outcome FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[:] == ("completed", "restarted")


def test_service_claims_exact_prepared_successor_and_acquires_actor_lease(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    _abandon(conn, source)
    prepared = settle_abandonment_frontier(
        conn, backend, abandonment_id="abandonment", reason="gone"
    )
    successor_id = prepared["successor_operation_id"]
    conn.close()

    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=tmp_path),
        backend_factory=lambda: backend,
        release_loader=lambda role=None: _release(role or "planning"),
    )

    blocked = confirmed_planning_start(
        service,
        {
            "task_gid": "task",
            "agent": "gpt",
            "kind": "planning",
            "prepared_operation_id": successor_id,
        },
        principal=ServicePrincipal("owner", "dead-run"),
        challenge_request_id=str(uuid.uuid4()),
        start_request_id=str(uuid.uuid4()),
    )
    assert blocked["ok"] is False
    assert blocked["errors"][0]["rule"] == "abandoned_run_claim_forbidden"

    result = confirmed_planning_start(
        service,
        {
            "task_gid": "task",
            "agent": "gpt",
            "kind": "planning",
            "prepared_operation_id": successor_id,
        },
        principal=ServicePrincipal("owner", "fresh-run"),
        challenge_request_id=str(uuid.uuid4()),
        start_request_id=str(uuid.uuid4()),
    )
    assert result["ok"] is True
    assert result["submission_id"] == successor_id

    check = initialize_database(db_path)
    lease = check.execute(
        "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
        (successor_id,),
    ).fetchone()
    assert lease is not None
    assert lease["owner_id"] == "owner"
    assert lease["run_id"] == "fresh-run"
    assert lease["lease_kind"] == "actor"
    check.close()


def test_start_schema_accepts_prepared_stage_target_but_not_verification():
    client = {
        "run_id": "22222222-2222-4222-8222-222222222222",
        "request_id": "33333333-3333-4333-8333-333333333333",
    }
    validate_action_request(
        "start",
        {
            "client": client,
            "arguments": {
                "task_gid": "123456789",
                "agent": "gpt",
                "kind": "initial",
                "prepared_operation_id": "11111111-1111-4111-8111-111111111111",
            },
        },
    )
    with pytest.raises(DishRuleError) as rejected_prepared_target:
        validate_action_request(
            "start",
            {
                "client": client,
                "arguments": {
                    "task_gid": "123456789",
                    "agent": "gpt",
                    "kind": "verification",
                    "independence_attestation": "I independently reviewed the candidate.",
                    "prepared_operation_id": "11111111-1111-4111-8111-111111111111",
                },
            },
        )
    assert rejected_prepared_target.value.rule == "argument_unexpected"
