from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from dish_service.application import DishService
from dish_service.command_spec import validate_action_request
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.abandonment import settle_abandonment_frontier
from dish_tool.commands import DishApplication
from dish_tool.database import create_abandonment_attempt_in_transaction
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import ResolvedRelease
from tests._workflow_builders import create_large_rejection_successor, reject_large
from tests.support.verification import TASK, make_app


def _release(root: Path, role: str | None = None) -> ResolvedRelease:
    verification = "# Exact frozen Verification protocol\n"
    return ResolvedRelease(
        version="1.0.10",
        commit="",
        root=root,
        protocols={} if role is None else {
            role: verification if role == "verification" else f"{role} protocol"
        },
        manifests={},
        manifest_texts={},
        schema_version="2",
        schema={},
        schema_text="{}",
        migration_metadata={},
        requested_protocol_role=role,
    )




def _prepared_verification(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="dead-verifier-run",
        independence_attestation="independent",
    )
    assert review["ok"]
    cycle_id = review["data"]["cycle_id"]
    lease = LeaseManager(app.conn).acquire(
        operation_id,
        ServicePrincipal("owner", "dead-verifier-run"),
        context_cycle_id=cycle_id,
    )
    LeaseManager(app.conn).release(
        operation_id, None, reason="stale verifier released", admin=True
    )
    app.conn.execute("BEGIN IMMEDIATE")
    try:
        create_abandonment_attempt_in_transaction(
            app.conn,
            abandonment_id="abandonment",
            task_gid="t",
            source_operation_id=operation_id,
            source_lease_id=lease["lease_id"],
            abandoned_owner_id="owner",
            abandoned_run_id="dead-verifier-run",
            attempt_cycle_id=cycle_id,
            reason="conversation permanently unavailable",
        )
        app.conn.execute("COMMIT")
    except Exception:
        app.conn.execute("ROLLBACK")
        raise
    prepared = settle_abandonment_frontier(
        app.conn,
        backend,
        abandonment_id="abandonment",
        reason="conversation permanently unavailable",
    )
    return app, backend, operation_id, cycle_id, prepared


@pytest.mark.smoke
def test_clean_verification_abandonment_creates_exact_unbound_successor(tmp_path):
    app, _backend, source_id, source_cycle_id, prepared = _prepared_verification(
        tmp_path
    )
    successor_id = prepared["successor_operation_id"]
    successor_cycle_id = prepared["successor_cycle_id"]

    assert prepared["required_action"] == {
        "surface": "connected-agent",
        "command": "start",
        "arguments": {
            "task_gid": "t",
            "kind": "verification",
            "target_operation_id": successor_id,
            "target_cycle_id": successor_cycle_id,
        },
    }
    assert app.conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (source_id,),
    ).fetchone()[:] == ("cancelled", "terminal", "agent_abandoned")
    source_cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id=?", (source_cycle_id,)
    ).fetchone()
    assert source_cycle["outcome"] == "abandoned"
    assert source_cycle["completed_at"] is not None
    assert source_cycle["signed_identity"] is None

    successor = app.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (successor_id,)
    ).fetchone()
    fresh_cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id=?", (successor_cycle_id,)
    ).fetchone()
    assert successor["status"] == "open"
    assert successor["phase"] == "await_verification"
    assert successor["successor_claim_mode"] == "verifier"
    assert successor["run_id"] == "constructor-run"
    assert successor["verifier_agent"] is None
    assert fresh_cycle["operation_id"] == successor_id
    assert fresh_cycle["verifier_agent"] is None
    assert fresh_cycle["run_id"] is None
    assert fresh_cycle["reviewed_identity"] is None
    assert fresh_cycle["protocol_release"] == source_cycle["protocol_release"]
    roles = {
        row["role"]
        for row in app.conn.execute(
            "SELECT role FROM operation_actor_facts WHERE operation_id=?",
            (successor_id,),
        )
    }
    assert "constructor" in roles
    assert "verifier" not in roles


@pytest.mark.smoke
def test_prepared_verification_resolves_target_and_requires_fresh_run(tmp_path):
    app, _backend, _source_id, _source_cycle_id, prepared = _prepared_verification(
        tmp_path
    )
    successor_id = prepared["successor_operation_id"]
    successor_cycle_id = prepared["successor_cycle_id"]

    old_run = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="dead-verifier-run",
        independence_attestation="independent",
    )
    assert old_run["code"] == "AGENT_MISMATCH"
    assert old_run["errors"][0]["rule"] == "abandoned_run_claim_forbidden"

    stale = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="fresh-verifier-run",
        independence_attestation="independent",
        target_operation_id=successor_id,
        target_cycle_id=str(uuid.uuid4()),
    )
    assert stale["code"] == "WRONG_STATE"
    assert stale["errors"][0]["rule"] == "verification_start_target_stale"

    claimed = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="fresh-verifier-run",
        independence_attestation="independent",
    )
    assert claimed["ok"]
    assert claimed["submission_id"] == successor_id
    assert claimed["data"]["cycle_id"] == successor_cycle_id
    assert app.conn.execute(
        "SELECT successor_claim_mode,verifier_agent FROM operations WHERE operation_id=?",
        (successor_id,),
    ).fetchone()[:] == ("none", "codex")
    assert app.conn.execute(
        "SELECT status,outcome FROM abandonment_attempts WHERE abandonment_id='abandonment'"
    ).fetchone()[:] == ("completed", "restarted")


@pytest.mark.smoke
def test_service_claims_exact_verification_target_and_binds_lease_cycle(tmp_path):
    app, backend, _source_id, _source_cycle_id, prepared = _prepared_verification(
        tmp_path
    )
    successor_id = prepared["successor_operation_id"]
    successor_cycle_id = prepared["successor_cycle_id"]
    app.conn.close()

    service = DishService(
        ServiceConfig(db_path=tmp_path / "dish.db", honest_root=tmp_path / "honest"),
        backend_factory=lambda: backend,
        release_loader=lambda role=None: _release(tmp_path / "honest", role),
    )
    request_id = str(uuid.uuid4())
    result = service.execute_agent(
        "start",
        {
            "task_gid": "t",
            "agent": "codex",
            "kind": "verification",
            "independence_attestation": "independent",
            "target_operation_id": successor_id,
            "target_cycle_id": successor_cycle_id,
        },
        principal=ServicePrincipal("owner", "fresh-service-run"),
        request_id=request_id,
    )
    assert result["ok"]
    replay = service.execute_agent(
        "start",
        {
            "task_gid": "t",
            "agent": "codex",
            "kind": "verification",
            "independence_attestation": "independent",
            "target_operation_id": successor_id,
            "target_cycle_id": successor_cycle_id,
        },
        principal=ServicePrincipal("owner", "fresh-service-run"),
        request_id=request_id,
    )
    assert replay["ok"] and replay["submission_id"] == result["submission_id"]
    assert replay["data"]["cycle_id"] == successor_cycle_id
    assert replay["data"]["request_replayed"] is True

    check = initialize_database(tmp_path / "dish.db")
    lease = check.execute(
        "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
        (successor_id,),
    ).fetchone()
    assert lease is not None
    assert lease["run_id"] == "fresh-service-run"
    assert lease["context_cycle_id"] == successor_cycle_id
    check.close()


@pytest.mark.smoke
def test_route_preserved_verification_continuation_resolves_omitted_target(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="dead-verifier-run",
        independence_attestation="independent",
    )
    old_cycle_id = review["data"]["cycle_id"]
    next_cycle = create_large_rejection_successor(
        app,
        tmp_path,
        operation_id=operation_id,
        agent="codex",
        run_id="dead-verifier-run",
        candidate_text=TASK.replace("100 g", "120 g"),
    )
    lease = LeaseManager(app.conn).acquire(
        operation_id,
        ServicePrincipal("owner", "dead-verifier-run"),
        context_cycle_id=old_cycle_id,
    )
    LeaseManager(app.conn).release(
        operation_id, None, reason="stale verifier released", admin=True
    )
    app.conn.execute("BEGIN IMMEDIATE")
    create_abandonment_attempt_in_transaction(
        app.conn,
        abandonment_id="abandonment",
        task_gid="t",
        source_operation_id=operation_id,
        source_lease_id=lease["lease_id"],
        abandoned_owner_id="owner",
        abandoned_run_id="dead-verifier-run",
        attempt_cycle_id=old_cycle_id,
        reason="conversation permanently unavailable",
    )
    app.conn.execute("COMMIT")

    settle_abandonment_frontier(
        app.conn, backend, abandonment_id="abandonment", reason="preserve route"
    )

    old_run = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="verification",
        run_id="dead-verifier-run",
        independence_attestation="independent",
    )
    assert old_run["errors"][0]["rule"] == "abandoned_run_claim_forbidden"

    claimed = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="verification",
        run_id="fresh-run",
        independence_attestation="independent",
    )
    assert claimed["ok"]
    assert claimed["submission_id"] == operation_id
    assert claimed["data"]["cycle_id"] == next_cycle["cycle_id"]


@pytest.mark.smoke
def test_route_preserved_verification_continuation_is_exact_targeted(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="dead-verifier-run",
        independence_attestation="independent",
    )
    old_cycle_id = review["data"]["cycle_id"]
    next_cycle = create_large_rejection_successor(
        app,
        tmp_path,
        operation_id=operation_id,
        agent="codex",
        run_id="dead-verifier-run",
        candidate_text=TASK.replace("100 g", "120 g"),
    )
    lease = LeaseManager(app.conn).acquire(
        operation_id,
        ServicePrincipal("owner", "dead-verifier-run"),
        context_cycle_id=old_cycle_id,
    )
    LeaseManager(app.conn).release(
        operation_id, None, reason="stale verifier released", admin=True
    )
    app.conn.execute("BEGIN IMMEDIATE")
    create_abandonment_attempt_in_transaction(
        app.conn,
        abandonment_id="abandonment",
        task_gid="t",
        source_operation_id=operation_id,
        source_lease_id=lease["lease_id"],
        abandoned_owner_id="owner",
        abandoned_run_id="dead-verifier-run",
        attempt_cycle_id=old_cycle_id,
        reason="conversation permanently unavailable",
    )
    app.conn.execute("COMMIT")

    result = settle_abandonment_frontier(
        app.conn, backend, abandonment_id="abandonment", reason="preserve route"
    )
    assert result["required_action"]["arguments"] == {
        "task_gid": "t",
        "kind": "verification",
        "target_operation_id": operation_id,
        "target_cycle_id": next_cycle["cycle_id"],
    }

    claimed = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="verification",
        run_id="fresh-run",
        independence_attestation="independent",
        target_operation_id=operation_id,
        target_cycle_id=next_cycle["cycle_id"],
    )
    assert claimed["ok"], claimed
    live_candidate = f"{backend.title}\n{backend.notes}".replace("120 g", "130 g")
    held = reject_large(
        app,
        tmp_path,
        operation_id=operation_id,
        agent="gpt",
        run_id="fresh-run",
        candidate_text=live_candidate,
    )
    assert held["data"]["two_pass_hold"] is True

    reopened_text = f"{backend.title}\n{backend.notes}".replace(
        "Compare hydration routes.",
        "Compare hydration routes after a rested-starch reset.",
    )
    reopened_candidate = tmp_path / "reopened-route.txt"
    reopened_candidate.write_text(reopened_text, encoding="utf-8")
    reopened = DishAdminApplication(app.conn, backend=backend).execute(
        "reopen",
        submission_id=operation_id,
        category="premise",
        before="Compare hydration routes.",
        after="Compare hydration routes after a rested-starch reset.",
        editor="codex",
        model="gpt-5.6-sol",
        run_id="reopen-run",
        file_path=str(reopened_candidate),
        date="2026-07-31",
    )
    assert reopened["ok"], reopened
    later = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()
    assert later is not None

    stale = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="later-run",
        independence_attestation="independent",
        target_operation_id=operation_id,
        target_cycle_id=next_cycle["cycle_id"],
    )
    assert stale["errors"][0]["rule"] == "verification_start_target_stale"
    assert app.conn.execute(
        "SELECT run_id FROM verification_cycles WHERE cycle_id=?", (later["cycle_id"],)
    ).fetchone()[0] is None


@pytest.mark.smoke
def test_action_schema_accepts_only_complete_verification_target_pair():
    client = {
        "run_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
    }
    operation_id = str(uuid.uuid4())
    cycle_id = str(uuid.uuid4())
    validate_action_request(
        "start",
        {
            "client": client,
            "arguments": {
                "task_gid": "123456789",
                "agent": "gpt",
                "kind": "verification",
                "independence_attestation": "independent",
                "target_operation_id": operation_id,
                "target_cycle_id": cycle_id,
            },
        },
    )
    with pytest.raises(DishRuleError):
        validate_action_request(
            "start",
            {
                "client": client,
                "arguments": {
                    "task_gid": "123456789",
                    "agent": "gpt",
                    "kind": "verification",
                    "independence_attestation": "independent",
                    "target_operation_id": operation_id,
                },
            },
        )
    with pytest.raises(DishRuleError):
        validate_action_request(
            "start",
            {
                "client": client,
                "arguments": {
                    "task_gid": "123456789",
                    "agent": "gpt",
                    "kind": "initial",
                    "target_operation_id": operation_id,
                    "target_cycle_id": cycle_id,
                },
            },
        )
