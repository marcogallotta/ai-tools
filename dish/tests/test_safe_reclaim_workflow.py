from __future__ import annotations

import json
import uuid

import pytest

from dish_service.application import DishService
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_service.request_replay import (
    begin_request,
    complete_request,
    settle_resolved_operation_requests,
)
from dish_tool.database import (
    create_abandonment_attempt_in_transaction,
    record_marco_authorization,
    revoke_operation_run_in_transaction,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.operation_execution import (
    claim_operation_execution,
    execution_recovery_state,
    finish_operation_execution,
)
from dish_tool.safe_reclaim import execute_safe_reclaim
from dish_tool.transactions import immediate_transaction
from tests.support.abandonment import Backend as AbandonmentBackend
from tests.support.lease_authority import _principal, _service, _start
from tests.support.service_leases import Clock
from tests.support.verification import TASK
from tests.support.abandonment import _NUMERIC_TASK_GID, _numeric_task_source


def _verification_with_expired_lease(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=30)
    constructor = _principal("action", "constructor-run")
    started = _start(service, constructor)
    operation_id = started["submission_id"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=constructor,
    )
    assert prepared["ok"]
    verifier = _principal("action", "verifier-run")
    reviewed = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent verification run",
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert reviewed["ok"]
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["allowed_actions"] == ["approve", "reject"]
    clock.advance(31)
    return service, verifier, operation_id, reviewed["data"]["reviewed_identity"]


def _strand_service_request_after_resolved_execution(service, operation_id, verifier):
    request_id = str(uuid.uuid4())
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn,
            request_id=request_id,
            owner_id=verifier.owner_id,
            run_id=verifier.run_id,
            command="reject",
            arguments={
                "submission_id": operation_id,
                "route": "large",
                "reason": "test",
            },
        )
        claim = claim_operation_execution(
            conn, operation_id=operation_id, command="reject", request_id=request_id
        )
        evidence = execution_recovery_state(conn, execution_id=claim.execution_id)
        finish_operation_execution(conn, claim, status="uncertain", evidence=evidence)
        complete_request(
            conn,
            request_id=request_id,
            result={
                "ok": False,
                "code": "BACKEND_UNCERTAIN",
                "command": "reject",
                "task_gid": "t",
                "submission_id": operation_id,
                "state": "open",
                "retryable": True,
                "allowed_actions": [],
                "data": {},
                "errors": [],
            },
        )
        resumed = claim_operation_execution(
            conn, operation_id=operation_id, command="reject", request_id=request_id
        )
        resolved = execution_recovery_state(
            conn, execution_id=resumed.execution_id, refresh=True
        )
        finish_operation_execution(conn, resumed, status="completed", evidence=resolved)
        request = conn.execute(
            "SELECT status,resolved_at FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        execution = conn.execute(
            "SELECT status,resolved_at,resolution_evidence_json FROM operation_executions WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert tuple(request) == ("uncertain", None)
        assert execution["status"] == "completed"
        assert execution["resolved_at"] is not None
        assert execution["resolution_evidence_json"] is not None
        return request_id
    finally:
        conn.close()


def _fresh_reclaimable_verification(tmp_path):
    service, old_verifier, operation_id, _identity = _verification_with_expired_lease(
        tmp_path
    )
    fresh = _principal("action", "fresh-verifier-run")
    discovered = service.execute_agent(
        "read",
        {"agent": "claude", "task_gid": "t"},
        principal=fresh,
    )
    assert discovered["ok"] is True
    assert discovered["allowed_actions"] == ["safe-reclaim"]
    assert discovered["data"]["service_access"]["state"] == "safe_reclaim_available"
    action = discovered["data"]["agent_action"]
    assert action["command"] == "safe-reclaim"
    assert action["arguments"]["agent"] == "claude"
    return service, old_verifier, fresh, operation_id, action["arguments"]["lease_id"]

def test_different_run_can_safe_reclaim_clean_expired_verification_attempt(tmp_path):
    service, old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(tmp_path)
    request_id = str(uuid.uuid4())

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {
            "agent": "claude",
            "submission_id": operation_id,
            "lease_id": lease_id,
        },
        principal=fresh,
        request_id=request_id,
    )

    assert reclaimed["ok"] is True
    assert reclaimed["state"] == "reclaimed"
    assert reclaimed["allowed_actions"] == ["start"]
    data = reclaimed["data"]
    successor_operation_id = data["successor_operation_id"]
    successor_cycle_id = data["successor_cycle_id"]
    assert data["source_operation_id"] == operation_id
    assert data["source_lease_id"] == lease_id
    assert data["previous_run_id"] == old_verifier.run_id
    assert data["agent_action"] == {
        "command": "start",
        "arguments": {
            "task_gid": "t",
            "kind": "verification",
            "target_operation_id": successor_operation_id,
            "target_cycle_id": successor_cycle_id,
        },
    }

    conn = initialize_database(service.config.db_path)
    try:
        source = conn.execute(
            "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        successor = conn.execute(
            "SELECT status,phase,successor_claim_mode FROM operations WHERE operation_id=?",
            (successor_operation_id,),
        ).fetchone()
        lineage = conn.execute(
            "SELECT * FROM safe_reclaims WHERE source_operation_id=?",
            (operation_id,),
        ).fetchone()
        source_cycle = conn.execute(
            "SELECT outcome,completed_at FROM verification_cycles WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(source) == ("cancelled", "terminal", "safe_reclaimed")
        assert tuple(successor) == ("open", "await_verification", "verifier")
        assert lineage["status"] == "prepared"
        assert lineage["successor_operation_id"] == successor_operation_id
        assert lineage["successor_cycle_id"] == successor_cycle_id
        assert tuple(source_cycle) == ("safe_reclaimed", source_cycle["completed_at"])
        assert source_cycle["completed_at"] is not None
    finally:
        conn.close()

    old_attempt = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "target_operation_id": successor_operation_id,
            "target_cycle_id": successor_cycle_id,
            "independence_attestation": "independent verification run",
        },
        principal=old_verifier,
        request_id=str(uuid.uuid4()),
    )
    assert old_attempt["code"] == "AGENT_MISMATCH"
    assert old_attempt["errors"][0]["rule"] == "safe_reclaim_previous_run_forbidden"

    resumed = service.execute_agent(
        "start",
        {
            "agent": "claude",
            "task_gid": "t",
            "kind": "verification",
            "target_operation_id": successor_operation_id,
            "target_cycle_id": successor_cycle_id,
            "independence_attestation": "independent verification run",
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert resumed["ok"] is True

    conn = initialize_database(service.config.db_path)
    try:
        lineage = conn.execute(
            "SELECT status,claimed_at FROM safe_reclaims WHERE source_operation_id=?",
            (operation_id,),
        ).fetchone()
        assert lineage["status"] == "claimed"
        assert lineage["claimed_at"] is not None
    finally:
        conn.close()



def test_safe_reclaim_preserves_unused_marco_authorization_with_provenance(tmp_path):
    service, _old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        granted = record_marco_authorization(
            conn,
            task_gid="t",
            operation_id=operation_id,
            field_name="Exemptions",
            before="None",
            after="[nutrition-fat] — Marco-approved exact exception",
            reason="Marco approved the exact exception",
            actor_run_id="marco-run",
        )
    finally:
        conn.close()

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {
            "agent": "claude",
            "submission_id": operation_id,
            "lease_id": lease_id,
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert reclaimed["ok"] is True
    successor_operation_id = reclaimed["data"]["successor_operation_id"]

    conn = initialize_database(service.config.db_path)
    try:
        inherited = conn.execute(
            """SELECT * FROM marco_authorizations
                 WHERE operation_id=? AND field_name='Exemptions'""",
            (successor_operation_id,),
        ).fetchall()
        assert len(inherited) == 1
        assert inherited[0]["authorization_id"] != granted["authorization_id"]
        assert inherited[0]["before_json"] == granted["before_json"]
        assert inherited[0]["after_json"] == granted["after_json"]
        audit = conn.execute(
            """SELECT details,actor_provenance FROM audit_events
                 WHERE operation_id=? AND event_type='marco.authorization'
                 ORDER BY created_at DESC LIMIT 1""",
            (successor_operation_id,),
        ).fetchone()
        details = json.loads(audit["details"])
        provenance = json.loads(audit["actor_provenance"])
        assert details["inherited_from_authorization_id"] == granted["authorization_id"]
        assert details["safe_reclaim_id"] == reclaimed["data"]["safe_reclaim_id"]
        assert provenance["source"] == "safe-reclaim"
    finally:
        conn.close()

def test_same_expired_run_gets_connected_renewal_not_safe_reclaim(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    inspected = service.execute_agent(
        "read", {"agent": "codex", "task_gid": "t"}, principal=verifier
    )
    assert inspected["allowed_actions"] == ["renew-lease"]
    assert inspected["data"].get("required_admin_action") is None
    assert inspected["data"]["service_access"]["state"] == "expired_same_run_revivable"
    assert inspected["data"]["agent_action"] == {
        "command": "renew-lease",
        "arguments": {"operation_id": operation_id},
    }
    assert "safe-reclaim" not in inspected["data"].get("legal_next_actions", [])


def test_original_run_revival_wins_before_fresh_safe_reclaim(tmp_path):
    service, old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(tmp_path)

    revived = service.renew_lease(operation_id, old_verifier, request_id=str(uuid.uuid4()))
    assert revived["ok"] is True
    assert revived["data"]["service_lease"]["lease_id"] == lease_id

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {"agent": "claude", "submission_id": operation_id, "lease_id": lease_id},
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert reclaimed["ok"] is False
    assert reclaimed["errors"][0]["rule"] == "safe_reclaim_not_eligible"
    failed = {
        item["rule"]
        for item in reclaimed["errors"][0]["eligibility"]["failed_clauses"]
    }
    assert "safe_reclaim_live_lease" in failed


def test_fresh_safe_reclaim_wins_before_original_run_revival(tmp_path):
    service, old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(tmp_path)

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {"agent": "claude", "submission_id": operation_id, "lease_id": lease_id},
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert reclaimed["ok"] is True

    revived = service.renew_lease(operation_id, old_verifier, request_id=str(uuid.uuid4()))
    assert revived["ok"] is False
    assert revived["code"] == "WRONG_STATE"
    assert revived["errors"][0]["rule"] == "operation_not_open"


def test_revoked_expired_original_run_cannot_revive(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
        with immediate_transaction(conn, "test_revoke_expired_original_run"):
            revoke_operation_run_in_transaction(
                conn,
                operation_id=operation_id,
                owner_id=verifier.owner_id,
                run_id=verifier.run_id,
                source_lease_id=lease["lease_id"],
                reason="test explicit kill",
            )
    finally:
        conn.close()

    revived = service.renew_lease(operation_id, verifier, request_id=str(uuid.uuid4()))
    assert revived["ok"] is False
    assert revived["code"] == "AGENT_MISMATCH"
    assert revived["errors"][0]["rule"] == "killed_run_revoked"


def test_abandoned_expired_original_run_cannot_revive(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
        with immediate_transaction(conn, "test_abandon_expired_original_run"):
            create_abandonment_attempt_in_transaction(
                conn,
                abandonment_id=str(uuid.uuid4()),
                task_gid=lease["task_gid"],
                source_operation_id=operation_id,
                source_lease_id=lease["lease_id"],
                abandoned_owner_id=verifier.owner_id,
                abandoned_run_id=verifier.run_id,
                attempt_cycle_id=lease["context_cycle_id"],
                reason="test conversation permanently unavailable",
                created_at=lease["expires_at"],
            )
    finally:
        conn.close()

    revived = service.renew_lease(operation_id, verifier, request_id=str(uuid.uuid4()))
    assert revived["ok"] is False
    assert revived["code"] == "AGENT_MISMATCH"
    error = revived["errors"][0]
    assert error["rule"] == "service_lease_revival_superseded"
    assert error["abandonment_status"] == "started"


def test_unresolved_consequential_execution_blocks_same_run_revival(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        claim_operation_execution(
            conn, operation_id=operation_id, command="approve", request_id=None
        )
    finally:
        conn.close()

    revived = service.renew_lease(operation_id, verifier, request_id=str(uuid.uuid4()))
    assert revived["ok"] is False
    assert revived["code"] == "WRONG_STATE"
    assert revived["errors"][0]["rule"] == "service_lease_revival_recovery_required"
    assert "execution_claim" in revived["errors"][0]
    assert "unresolved_executions" in revived["errors"][0]


def test_unresolved_consequential_request_blocks_same_run_revival(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    stranded_request_id = _strand_service_request_after_resolved_execution(
        service, operation_id, verifier
    )

    revived = service.renew_lease(operation_id, verifier, request_id=str(uuid.uuid4()))

    assert revived["ok"] is False
    assert revived["code"] == "WRONG_STATE"
    error = revived["errors"][0]
    assert error["rule"] == "service_lease_revival_recovery_required"
    assert [item["request_id"] for item in error["unresolved_requests"]] == [
        stranded_request_id
    ]


def test_prepare_required_original_run_resumes_without_admin_recovery(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=30)
    original = _principal("action", "29dfd41e-2942-4620-919a-c4b624b63ad8")
    started = _start(service, original)
    operation_id = started["submission_id"]
    original_lease_id = started["data"]["service_lease"]["lease_id"]
    clock.advance(31)

    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=original,
        request_id=str(uuid.uuid4()),
    )

    assert prepared["ok"] is True
    assert prepared["submission_id"] == operation_id
    assert prepared["data"].get("required_admin_action") is None
    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (original_lease_id,)
        ).fetchone()
        operation = conn.execute(
            "SELECT status,phase FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        assert lease["run_id"] == original.run_id
        assert lease["release_reason"].startswith("workflow_handoff:")
        assert tuple(operation) == ("open", "await_verification")
    finally:
        conn.close()


def test_safe_reclaim_exact_request_replays_after_commit(tmp_path):
    service, _old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(tmp_path)
    request_id = str(uuid.uuid4())
    arguments = {
        "agent": "claude",
        "submission_id": operation_id,
        "lease_id": lease_id,
    }
    first = service.execute_agent(
        "safe-reclaim", arguments, principal=fresh, request_id=request_id
    )
    assert first["ok"] is True

    restarted = DishService(
        service.config,
        backend_factory=service.backend_factory,
        release_loader=service.release_loader,
        lease_now=service.lease_now,
    )
    replay = restarted.execute_agent(
        "safe-reclaim", arguments, principal=fresh, request_id=request_id
    )
    assert replay["ok"] is True
    assert replay["data"]["request_replayed"] is True
    assert replay["data"]["request_id"] == request_id
    assert replay["data"]["safe_reclaim_id"] == first["data"]["safe_reclaim_id"]


def test_unsettled_execution_blocks_reclaim_and_admin_points_to_recovery(tmp_path):
    from dish_tool.admin import DishAdminApplication
    from dish_tool.operation_execution import claim_operation_execution

    service, _old_verifier, fresh, operation_id, _lease_id = _fresh_reclaimable_verification(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        claim_operation_execution(
            conn,
            operation_id=operation_id,
            command="approve",
            request_id=None,
        )
    finally:
        conn.close()

    discovered = service.execute_agent(
        "read",
        {"agent": "claude", "task_gid": "t"},
        principal=fresh,
    )
    assert "safe-reclaim" not in discovered["allowed_actions"]
    assert discovered["data"]["required_admin_action"] == "inspect"
    failed_rules = {
        item["rule"] for item in discovered["data"]["safe_reclaim"]["failed_clauses"]
    }
    assert "safe_reclaim_execution_claim_live" in failed_rules
    assert "safe_reclaim_execution_unsettled" in failed_rules

    conn = initialize_database(service.config.db_path)
    try:
        admin = DishAdminApplication(conn, backend=service.backend_factory())
        inspected = admin.execute("inspect", submission_id=operation_id)
    finally:
        conn.close()
    assert inspected["ok"] is True
    human_actions = inspected["data"]["human_actions"]
    commands = {item.get("command") for item in human_actions}
    assert "recover" in commands
    assert "kill" not in commands
    assert "abandon-operation" not in commands


def test_safe_reclaim_restarts_clean_preconstruction_stage_with_linked_successor(tmp_path):
    from tests.support.lease_authority import _service, _start
    from tests.support.service_leases import Clock

    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=30)
    old = _principal("action", "old-constructor-run")
    started = _start(service, old)
    operation_id = started["submission_id"]
    clock.advance(31)

    fresh = _principal("action", "fresh-constructor-run")
    discovered = service.execute_agent(
        "read", {"agent": "claude", "task_gid": "t"}, principal=fresh
    )
    assert discovered["allowed_actions"] == ["safe-reclaim"]
    action = discovered["data"]["agent_action"]

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {
            "agent": "claude",
            "submission_id": operation_id,
            "lease_id": action["arguments"]["lease_id"],
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert reclaimed["ok"] is True
    assert reclaimed["data"]["stage"] == "research"
    successor = reclaimed["data"]["successor_operation_id"]
    assert reclaimed["data"]["agent_action"] == {
        "command": "start",
        "arguments": {
            "task_gid": "t",
            "kind": "initial",
            "prepared_operation_id": successor,
        },
    }

    resumed = service.execute_agent(
        "start",
        {
            "agent": "claude",
            "task_gid": "t",
            "kind": "initial",
            "prepared_operation_id": successor,
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert resumed["ok"] is True
    assert resumed["submission_id"] == successor


def test_revoked_run_cannot_safe_reclaim_an_inactive_attempt(tmp_path):
    conn = initialize_database(":memory:")
    backend = AbandonmentBackend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    operation_id = str(operation["operation_id"])
    revoked = ServicePrincipal("owner", "killed-reclaimer")
    killed_lease = LeaseManager(conn).acquire(operation_id, revoked)
    LeaseManager(conn).release(
        operation_id, None, reason="first run ended", admin=True
    )
    with immediate_transaction(conn, "test_revoke_safe_reclaimer"):
        revoke_operation_run_in_transaction(
            conn,
            operation_id=operation_id,
            owner_id=revoked.owner_id,
            run_id=revoked.run_id,
            source_lease_id=killed_lease["lease_id"],
            reason="test explicit kill",
        )

    later = ServicePrincipal("owner", "later-run")
    later_lease = LeaseManager(conn).acquire(operation_id, later)
    LeaseManager(conn).release(
        operation_id, None, reason="later run ended", admin=True
    )

    with pytest.raises(DishRuleError) as excinfo:
        execute_safe_reclaim(
            conn,
            backend,
            operation_id=operation_id,
            lease_id=later_lease["lease_id"],
            requested_owner_id=revoked.owner_id,
            requested_run_id=revoked.run_id,
            requested_agent="gpt",
            request_id=str(uuid.uuid4()),
        )

    assert excinfo.value.rule == "killed_run_revoked"
    source = conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert tuple(source) == ("open", "prepare_required", None)
    assert conn.execute(
        "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?", (operation_id,)
    ).fetchone() is None


def test_safe_reclaim_planning_successor_preserves_intent_gate_and_remains_claimable(tmp_path):
    from dish_service.config import ServiceConfig
    from tests.support.planning import Backend as PlanningBackend, release as planning_release
    from tests.support.planning_intent import confirmed_planning_start
    from tests.support.service_leases import Clock

    clock = Clock()
    backend = PlanningBackend(task_gid="t")
    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "planning.db",
            honest_root=honest,
            port=0,
            lease_ttl_seconds=30,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=lambda role=None: planning_release(honest, role),
        lease_now=clock.now,
    )
    old = _principal("action", "old-planning-run")
    planning_args = {"agent": "gpt", "task_gid": "t", "kind": "planning"}
    started = confirmed_planning_start(
        service,
        planning_args,
        principal=old,
        challenge_request_id=str(uuid.uuid4()),
        start_request_id=str(uuid.uuid4()),
    )
    assert started["ok"] is True
    clock.advance(31)

    fresh = _principal("action", "fresh-planning-run")
    discovered = service.execute_agent(
        "read", {"agent": "gpt", "task_gid": "t"}, principal=fresh
    )
    assert discovered["allowed_actions"] == ["safe-reclaim"]
    reclaim_action = discovered["data"]["agent_action"]
    reclaimed = service.execute_agent(
        "safe-reclaim",
        reclaim_action["arguments"],
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert reclaimed["ok"] is True
    assert reclaimed["data"]["stage"] == "planning"
    successor_action = reclaimed["data"]["agent_action"]
    assert successor_action["command"] == "start"
    assert successor_action["arguments"]["kind"] == "planning"

    # The safe-reclaim response already advertises the prepared successor. A
    # subsequent connected read must preserve that fresh-run continuation rather
    # than treating its intentionally absent lease/run as admin recovery.
    connected = service.execute_agent(
        "read", {"agent": "gpt", "task_gid": "t"}, principal=fresh
    )
    assert connected["allowed_actions"] == ["start"]
    assert connected["data"]["service_access"] == {"state": "claimable_by_run"}
    assert connected["data"].get("recovery_required") is not True
    assert connected["data"]["required_action"] == {
        "surface": "connected-agent",
        **successor_action,
    }

    challenge_request_id = str(uuid.uuid4())
    challenge = service.execute_agent(
        "start",
        {"agent": "gpt", **successor_action["arguments"]},
        principal=fresh,
        request_id=challenge_request_id,
    )
    assert challenge["code"] == "CONFIRMATION_REQUIRED"

    resumed = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            **successor_action["arguments"],
            "intent_challenge_id": challenge["data"]["intent_challenge_id"],
            "intent_basis": "user_requested",
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert resumed["ok"] is True
    assert resumed["submission_id"] == reclaimed["data"]["successor_operation_id"]


def test_safe_reclaim_change_successor_returns_exact_change_intent(tmp_path):
    from tests.support.lease_authority import _service
    from tests.support.service_leases import Clock

    clock = Clock()
    service, backend = _service(tmp_path, clock=clock, ttl=30)

    constructor = _principal("action", "constructor-run")
    initial = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert initial["ok"] is True
    assert service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": initial["submission_id"],
            "file_text": backend.title + "\n" + backend.notes,
        },
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )["ok"] is True
    verifier = _principal("action", "signoff-run")
    reviewed = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert reviewed["ok"] is True
    assert service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": initial["submission_id"]},
        principal=verifier,
    )["ok"] is True
    approved = service.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": initial["submission_id"],
            "correction": "none",
            "reviewed_identity": reviewed["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert approved["ok"] is True
    assert service.execute_agent(
        "submit",
        {"submission_id": initial["submission_id"]},
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )["ok"] is True

    old = _principal("action", "old-change-run")
    started = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "change",
            "change_level": "small",
            "change_reason": "correct one exact detail",
        },
        principal=old,
        request_id=str(uuid.uuid4()),
    )
    assert started["ok"] is True
    clock.advance(31)

    fresh = _principal("action", "fresh-change-run")
    discovered = service.execute_agent(
        "read", {"agent": "gpt", "task_gid": "t"}, principal=fresh
    )
    assert discovered["allowed_actions"] == ["safe-reclaim"]
    reclaimed = service.execute_agent(
        "safe-reclaim",
        discovered["data"]["agent_action"]["arguments"],
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert reclaimed["ok"] is True
    successor = reclaimed["data"]["successor_operation_id"]
    assert reclaimed["data"]["agent_action"] == {
        "command": "start",
        "arguments": {
            "task_gid": "t",
            "kind": "change",
            "prepared_operation_id": successor,
            "change_level": "small",
            "change_reason": "correct one exact detail",
        },
    }

    resumed = service.execute_agent(
        "start",
        {"agent": "claude", **reclaimed["data"]["agent_action"]["arguments"]},
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )
    assert resumed["ok"] is True
    assert resumed["submission_id"] == successor


def test_safe_reclaim_rechecks_verification_decision_state_inside_writer_transaction(
    tmp_path, monkeypatch
):
    import dish_tool.safe_reclaim as safe_reclaim_module

    service, _old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(
        tmp_path
    )
    original = safe_reclaim_module.safe_reclaim_eligibility
    calls = 0

    def raced_eligibility(conn, backend, **kwargs):
        nonlocal calls
        result = original(conn, backend, **kwargs)
        calls += 1
        if calls == 1 and result.eligible:
            conn.execute(
                """INSERT INTO operation_steps(operation_id,step_name,intended_json,completed_at)
                   VALUES (?,?,?,?)""",
                (operation_id, f"route_small:{result.source_cycle_id}", "{}", "2026-08-08T00:00:00Z"),
            )
        return result

    monkeypatch.setattr(safe_reclaim_module, "safe_reclaim_eligibility", raced_eligibility)

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {
            "agent": "claude",
            "submission_id": operation_id,
            "lease_id": lease_id,
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )

    assert reclaimed["code"] == "CONFLICT"
    assert reclaimed["errors"][0]["rule"] == "safe_reclaim_authority_changed"
    failed = {
        item["rule"]
        for item in reclaimed["errors"][0]["eligibility"]["failed_clauses"]
    }
    assert "safe_reclaim_verification_decision_started" in failed

    conn = initialize_database(service.config.db_path)
    try:
        source = conn.execute(
            "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(source) == ("open", "await_verification", None)
        assert conn.execute(
            "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?", (operation_id,)
        ).fetchone() is None
    finally:
        conn.close()


def test_safe_reclaim_rechecks_phase_frontier_inside_writer_transaction(
    tmp_path, monkeypatch
):
    import dish_tool.safe_reclaim as safe_reclaim_module

    service, _old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(
        tmp_path
    )
    original = safe_reclaim_module.safe_reclaim_eligibility
    calls = 0

    def raced_eligibility(conn, backend, **kwargs):
        nonlocal calls
        result = original(conn, backend, **kwargs)
        calls += 1
        if calls == 1 and result.eligible:
            conn.execute(
                "UPDATE operations SET phase='prepare_required' WHERE operation_id=?",
                (operation_id,),
            )
        return result

    monkeypatch.setattr(safe_reclaim_module, "safe_reclaim_eligibility", raced_eligibility)

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {
            "agent": "claude",
            "submission_id": operation_id,
            "lease_id": lease_id,
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )

    assert reclaimed["code"] == "CONFLICT"
    assert reclaimed["errors"][0]["rule"] == "safe_reclaim_authority_changed"
    conn = initialize_database(service.config.db_path)
    try:
        source = conn.execute(
            "SELECT status,terminal_outcome FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(source) == ("open", None)
        assert conn.execute(
            "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?", (operation_id,)
        ).fetchone() is None
    finally:
        conn.close()


def test_safe_reclaim_final_live_reread_rolls_back_before_source_is_consumed(tmp_path):
    service, _old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(
        tmp_path
    )
    backend = service.backend_factory()
    seen = 0

    def drift_after_initial_reclaim_read(*, result, gid):
        nonlocal seen
        seen += 1
        if seen == 1:
            backend.section = "rq"

    backend.after("read_task", drift_after_initial_reclaim_read)

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {
            "agent": "claude",
            "submission_id": operation_id,
            "lease_id": lease_id,
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )

    assert seen >= 2
    assert reclaimed["code"] == "CONFLICT"
    assert reclaimed["errors"][0]["rule"] == "safe_reclaim_authority_changed"
    failed = {
        item["rule"]
        for item in reclaimed["errors"][0]["eligibility"]["failed_clauses"]
    }
    assert "safe_reclaim_live_frontier_drift" in failed

    conn = initialize_database(service.config.db_path)
    try:
        source = conn.execute(
            "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        cycle = conn.execute(
            "SELECT outcome,completed_at FROM verification_cycles WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(source) == ("open", "await_verification", None)
        assert tuple(cycle) == (None, None)
        assert conn.execute(
            "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?", (operation_id,)
        ).fetchone() is None
    finally:
        conn.close()


def test_safe_reclaim_rechecks_current_verification_cycle_inside_writer_transaction(
    tmp_path, monkeypatch
):
    import dish_tool.safe_reclaim as safe_reclaim_module

    service, _old_verifier, fresh, operation_id, lease_id = _fresh_reclaimable_verification(
        tmp_path
    )
    original = safe_reclaim_module.safe_reclaim_eligibility
    calls = 0

    def raced_eligibility(conn, backend, **kwargs):
        nonlocal calls
        result = original(conn, backend, **kwargs)
        calls += 1
        if calls == 1 and result.eligible:
            replacement_cycle_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO verification_cycles(
                       cycle_id,operation_id,task_gid,cycle_number,protocol_release,
                       protocol_text,verifier_agent,run_id,independence_attestation,created_at
                   )
                   SELECT ?,operation_id,task_gid,cycle_number+1,protocol_release,
                          protocol_text,verifier_agent,?,independence_attestation,?
                     FROM verification_cycles WHERE cycle_id=?""",
                (
                    replacement_cycle_id,
                    "replacement-verifier-run",
                    "2026-08-08T00:00:00Z",
                    result.source_cycle_id,
                ),
            )
        return result

    monkeypatch.setattr(safe_reclaim_module, "safe_reclaim_eligibility", raced_eligibility)

    reclaimed = service.execute_agent(
        "safe-reclaim",
        {
            "agent": "claude",
            "submission_id": operation_id,
            "lease_id": lease_id,
        },
        principal=fresh,
        request_id=str(uuid.uuid4()),
    )

    assert reclaimed["code"] == "CONFLICT"
    assert reclaimed["errors"][0]["rule"] == "safe_reclaim_authority_changed"
    failed = {
        item["rule"]
        for item in reclaimed["errors"][0]["eligibility"]["failed_clauses"]
    }
    assert "safe_reclaim_lease_context_mismatch" in failed

    conn = initialize_database(service.config.db_path)
    try:
        source = conn.execute(
            "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(source) == ("open", "await_verification", None)
        assert conn.execute(
            "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?", (operation_id,)
        ).fetchone() is None
    finally:
        conn.close()


def test_resolved_execution_with_stranded_request_blocks_until_request_recovery(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    request_id = _strand_service_request_after_resolved_execution(
        service, operation_id, verifier
    )

    fresh = _principal("action", "fresh-after-stranded-request")
    discovered = service.execute_agent(
        "read", {"agent": "claude", "task_gid": "t"}, principal=fresh
    )

    assert discovered["ok"] is True
    assert "safe-reclaim" not in discovered["allowed_actions"]
    failed = {
        row["rule"] for row in discovered["data"]["safe_reclaim"]["failed_clauses"]
    }
    assert "safe_reclaim_request_unsettled" in failed
    conn = initialize_database(service.config.db_path)
    try:
        request = conn.execute(
            "SELECT status,resolved_at FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert tuple(request) == ("uncertain", None)
    finally:
        conn.close()


def test_recover_settles_stranded_request_then_returns_nonrecovery_continuation(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    request_id = _strand_service_request_after_resolved_execution(
        service, operation_id, verifier
    )
    admin = _principal("admin", "admin-recovery-run")
    lease_recovered = service.recover_lease(
        operation_id,
        admin,
        reason="prior verifier is inactive",
        request_id=str(uuid.uuid4()),
    )
    assert lease_recovered["ok"] is True

    recovered = service.execute_admin(
        "recover",
        {
            "submission_id": operation_id,
            "outcome": "inspect",
            "reason": "automatic inspection",
        },
        principal=admin,
        request_id=str(uuid.uuid4()),
    )

    assert recovered["ok"] is True
    assert recovered["allowed_actions"] == []
    assert request_id in recovered["data"]["settled_service_request_ids"]
    assert recovered["data"]["recovery_still_required"] is False
    post = recovered["data"]["post_recovery"]
    assert not any(
        action.get("command") == "recover"
        for action in post["human_actions"]
        if isinstance(action, dict)
    )

    conn = initialize_database(service.config.db_path)
    try:
        request = conn.execute(
            "SELECT status,resolution_result_json,resolved_at FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert request["status"] == "completed"
        assert request["resolved_at"] is not None
        resolution = json.loads(request["resolution_result_json"])
        assert resolution["command"] == "reject"
        assert resolution["code"] == "CONFLICT"
        assert resolution["data"]["request_recovery_resolved"] is True
        assert resolution["data"]["request_id"] == request_id
    finally:
        conn.close()


def test_request_recovery_does_not_settle_unresolved_request_bound_execution(tmp_path):
    service, verifier, operation_id, _identity = _verification_with_expired_lease(tmp_path)
    request_id = str(uuid.uuid4())
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn,
            request_id=request_id,
            owner_id=verifier.owner_id,
            run_id=verifier.run_id,
            command="reject",
            arguments={
                "submission_id": operation_id,
                "route": "large",
                "reason": "test",
            },
        )
        claim = claim_operation_execution(
            conn, operation_id=operation_id, command="reject", request_id=request_id
        )
        evidence = execution_recovery_state(conn, execution_id=claim.execution_id)
        finish_operation_execution(conn, claim, status="uncertain", evidence=evidence)
        complete_request(
            conn,
            request_id=request_id,
            result={
                "ok": False,
                "code": "BACKEND_UNCERTAIN",
                "command": "reject",
                "task_gid": "t",
                "submission_id": operation_id,
                "state": "open",
                "retryable": True,
                "allowed_actions": [],
                "data": {},
                "errors": [],
            },
        )
        premature = {
            "ok": False,
            "code": "CONFLICT",
            "command": "reject",
            "task_gid": "t",
            "submission_id": operation_id,
            "state": "open",
            "retryable": False,
            "allowed_actions": [],
            "data": {"message": "not resolved yet"},
            "errors": [],
        }
        authoritative = complete_request(
            conn, request_id=request_id, result=premature
        )
        assert authoritative["code"] == "BACKEND_UNCERTAIN"

        assert settle_resolved_operation_requests(conn, operation_id=operation_id) == []
        request = conn.execute(
            "SELECT status,resolved_at FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert tuple(request) == ("uncertain", None)
    finally:
        conn.close()
