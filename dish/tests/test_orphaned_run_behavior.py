"""Regression coverage for orphaned-operation recovery boundaries.

Clean expiry does not revoke the original durable run: that same run may
revive its actor lease and continue. A genuinely different run still needs
the governed safe-reclaim or admin recovery path before it can take over.
"""
from __future__ import annotations

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from tests.support.planning_intent import confirmed_planning_start
from tests.support.attestation import ATTESTATION
from tests.support.operational import Clock, _service
from tests.support.planning import Backend as PlanningBackend, PLANNING, release as planning_release
from tests.support.verification import TASK, make_app


def _planning_service(tmp_path, *, clock=None, ttl=60):
    backend = PlanningBackend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    (honest / "dish-verification-protocol.md").write_text("verification protocol")
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "managed-backups",
            lease_ttl_seconds=ttl,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
            port=0,
        ),
        backend_factory=lambda: backend,
        release_loader=lambda role=None: planning_release(honest, role),
        lease_now=None if clock is None else clock.now,
    )
    return service, backend


def _agent(owner: str, run_id: str) -> ServicePrincipal:
    return ServicePrincipal(owner_id=owner, run_id=run_id)


def _admin(run_id: str = "admin-run") -> ServicePrincipal:
    return ServicePrincipal(owner_id="admin", run_id=run_id)


def _prepare_args(operation_id: str, *, model: str = "m") -> dict:
    return {
        "agent": "gpt", "model": model, "submission_id": operation_id, "file_text": TASK,
    }


def _reject_large_args(operation_id: str, *, model: str = "m") -> dict:
    return {
        "agent": "codex", "model": model, "submission_id": operation_id,
        "route": "large", "reason": "material issue needs correction",
        "file_text": TASK,
    }


# ---------------------------------------------------------------------------
# 1. Research: lease released before `prepare`
# ---------------------------------------------------------------------------

def test_research_expired_same_run_transparently_revives_without_admin_recovery(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=60)
    run_a = _agent("gpt", "research-run-a")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=run_a,
    )
    assert started["ok"]
    operation_id = started["submission_id"]

    clock.advance(61)  # the lease row is still present; it has only time-expired

    same_run_attempt = service.execute_agent(
        "prepare", _prepare_args(operation_id), principal=run_a,
    )
    assert same_run_attempt["ok"], same_run_attempt
    assert same_run_attempt["data"].get("required_admin_action") is None


def test_research_fresh_run_is_permanently_locked_out_by_durable_run_id(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=60)
    run_a = _agent("gpt", "research-run-a")
    run_b = _agent("gpt", "research-run-b")  # a genuinely fresh run, never seen before
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=run_a,
    )
    operation_id = started["submission_id"]

    clock.advance(61)

    fresh_start_before_recovery = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=run_b,
    )
    assert fresh_start_before_recovery["code"] == "CONFLICT"
    assert fresh_start_before_recovery["errors"][0]["rule"] == "open_operation_exists"

    fresh_prepare_before_recovery = service.execute_agent(
        "prepare", _prepare_args(operation_id), principal=run_b,
    )
    assert fresh_prepare_before_recovery["code"] == "CONFLICT"
    assert fresh_prepare_before_recovery["errors"][0]["rule"] == "service_lease_expired"

    recovered = service.recover_lease(operation_id, _admin(), reason="test recovery")
    assert recovered["ok"]

    fresh_start_after_recovery = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=run_b,
    )
    assert fresh_start_after_recovery["code"] == "CONFLICT"
    assert fresh_start_after_recovery["errors"][0]["rule"] == "open_operation_exists"

    fresh_prepare_after_recovery = service.execute_agent(
        "prepare", _prepare_args(operation_id), principal=run_b,
    )
    assert fresh_prepare_after_recovery["code"] == "AGENT_MISMATCH"
    assert fresh_prepare_after_recovery["errors"][0]["rule"] == "service_lease_claim_forbidden"
    # The durable `operations.run_id` set at `start` (research-run-a), not the
    # lease, is what gates `prepare`. `recover-lease` clears the lease row but
    # never touches that column, so a run_id that never appears in it -- or in
    # operation_actor_facts -- can never claim this operation, ever.


# ---------------------------------------------------------------------------
# 2. Planning: same pattern as Research (kind="planning")
# ---------------------------------------------------------------------------

def _planning_prepare_args(operation_id: str) -> dict:
    return {
        "agent": "gpt", "model": "m", "submission_id": operation_id, "file_text": PLANNING,
    }


def test_planning_expired_same_run_transparently_revives_without_admin_recovery(tmp_path):
    clock = Clock()
    service, _backend = _planning_service(tmp_path, clock=clock, ttl=60)
    run_a = _agent("gpt", "planning-run-a")
    started = confirmed_planning_start(
        service,
        {"agent": "gpt", "task_gid": "t", "kind": "planning"},
        principal=run_a,
        challenge_request_id="11111111-1111-4111-8111-111111111111",
        start_request_id="22222222-2222-4222-8222-222222222222",
    )
    assert started["ok"], started
    operation_id = started["submission_id"]

    clock.advance(61)
    recovered_early = service.execute_agent(
        "prepare", _planning_prepare_args(operation_id), principal=run_a,
    )
    assert recovered_early["ok"], recovered_early
    assert recovered_early["data"].get("required_admin_action") is None


# ---------------------------------------------------------------------------
# 3. Verification: lease released before approve/reject
# ---------------------------------------------------------------------------

def test_verification_expired_same_run_revives_while_fresh_run_stays_locked_out(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=60)
    constructor = _agent("gpt", "constructor-run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=constructor,
    )
    operation_id = started["submission_id"]
    prepared = service.execute_agent(
        "prepare", _prepare_args(operation_id), principal=constructor,
    )
    assert prepared["ok"]

    verifier_a = _agent("codex", "verifier-run-a")
    review_a = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification",
         "independence_attestation": "independent"},
        principal=verifier_a,
    )
    assert review_a["ok"]
    inspected_a = service.execute_agent(
        "inspect", {"agent": "codex", "submission_id": operation_id}, principal=verifier_a,
    )
    assert inspected_a["ok"]

    clock.advance(61)

    revived = service.renew_lease(operation_id, verifier_a)
    assert revived["ok"], revived
    assert revived["data"]["service_lease"]["run_id"] == verifier_a.run_id

    verifier_b = _agent("codex", "verifier-run-b")
    fresh_start = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification",
         "independence_attestation": "independent"},
        principal=verifier_b,
    )
    assert fresh_start["code"] == "AGENT_MISMATCH"
    assert fresh_start["errors"][0]["rule"] == "service_lease_owner_mismatch"

    run_a_after_revival = service.execute_agent(
        "reject", _reject_large_args(operation_id), principal=verifier_a,
    )
    assert run_a_after_revival["ok"], run_a_after_revival


# ---------------------------------------------------------------------------
# 4. Human/Evidence hold: durability across lease expiry
# ---------------------------------------------------------------------------

def test_hold_resolution_survives_lease_expiry_and_original_run_resumes(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=60)
    researcher = _agent("gpt", "research-run-hold")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=researcher,
    )
    operation_id = started["submission_id"]

    held = service.execute_agent(
        "reject",
        {"agent": "gpt", "submission_id": operation_id, "route": "evidence",
         "reason": "Need authoritative source before construction",
         "resume_status": "pending-research"},
        principal=researcher,
    )
    assert held["ok"]
    # Entering a durable hold releases the actor lease immediately -- there is
    # no lease left to expire while the hold is open.
    assert held["data"]["service_lease"] is None

    clock.advance(600)  # far beyond the lease TTL; irrelevant to a hold

    recover_attempt = service.recover_lease(operation_id, _admin(), reason="probe")
    assert recover_attempt["code"] == "CONFLICT"
    assert recover_attempt["errors"][0]["rule"] == "service_lease_missing"

    resolved = service.execute_admin(
        "supply-evidence",
        {"submission_id": operation_id, "detail": "Required input supplied",
         "resume_status": "pending-research"},
        principal=_admin(),
    )
    assert resolved["ok"]
    # Admin's own request-scoped lease claim is released as part of resolving
    # the hold, so it does not itself block the next actor.
    assert resolved["data"]["service_lease"] is None

    original_run_resumes = service.execute_agent(
        "prepare", _prepare_args(operation_id), principal=researcher,
    )
    assert original_run_resumes["ok"]


def test_hold_resolution_does_not_grant_a_fresh_run_authority(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=60)
    researcher = _agent("gpt", "research-run-hold-2")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=researcher,
    )
    operation_id = started["submission_id"]

    held = service.execute_agent(
        "reject",
        {"agent": "gpt", "submission_id": operation_id, "route": "human-review",
         "reason": "Need Marco's decision before construction",
         "resume_status": "pending-research",
         "human_review_confirmed": True,
         "human_review_basis": "Only Marco can resolve the remaining choice within settled authority.",
         "repairs_considered": "Plausible within-authority repairs were considered and do not resolve the choice.",
         "human_review_options": [
             {"label": "Use Marco's chosen construction route", "decision": "Use Marco's chosen route before construction."}
         ]},
        principal=researcher,
    )
    assert held["ok"]

    clock.advance(600)

    resolved = service.execute_admin(
        "record-human-decision",
        {"submission_id": operation_id, "detail": "Marco decided to proceed",
         "resume_status": "pending-research"},
        principal=_admin(),
    )
    assert resolved["ok"]

    fresh_run = _agent("gpt", "fresh-run-post-hold")
    fresh_attempt = service.execute_agent(
        "prepare", _prepare_args(operation_id), principal=fresh_run,
    )
    assert fresh_attempt["code"] == "AGENT_MISMATCH"
    assert fresh_attempt["errors"][0]["rule"] == "service_lease_claim_forbidden"
    # Resolving the hold restores `prepare_required`, but authority is still
    # gated by the originating run/actor facts recorded before the hold, the
    # same durable binding that locks out a fresh run in Research/Planning.


# ---------------------------------------------------------------------------
# 5. Large-reject regression: commit 3d9a5b6 dropped only the
#    independence_attestation resupply requirement, not the ownership check
# ---------------------------------------------------------------------------

def test_large_reject_still_rejects_a_fresh_run_on_ownership_not_attestation(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    app.execute(
        "start", agent="codex", task_gid="t", kind="verification",
        run_id="verifier-a", independence_attestation=ATTESTATION,
    )
    assert app.execute("inspect", agent="codex", submission_id=operation_id)["ok"]
    candidate = tmp_path / "candidate.txt"
    candidate.write_text(TASK)

    rejected = app.execute(
        "reject", agent="codex", model="m", submission_id=operation_id,
        route="large", reason="method needs replacement",
        file_path=str(candidate), run_id="verifier-b",
    )

    assert rejected["code"] == "AGENT_MISMATCH"
    assert rejected["errors"][0]["rule"] == "verifier_proof_mismatch"
    # Not the independence_attestation regression this commit fixed --
    # verifier ownership/run-identity is still enforced.

    same_run_corrected = app.execute(
        "reject", agent="codex", model="m", submission_id=operation_id,
        route="large", reason="method needs replacement",
        file_path=str(candidate), run_id="verifier-a",
    )
    assert same_run_corrected["ok"]


def test_dead_verifier_failure_and_admin_inspect_return_same_safe_reclaim_action(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=60)
    constructor = _agent("gpt", "constructor-run-guidance")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=constructor,
    )
    operation_id = started["submission_id"]
    assert service.execute_agent(
        "prepare", _prepare_args(operation_id), principal=constructor,
    )["ok"]

    verifier_a = _agent("codex", "verifier-run-guidance-a")
    assert service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=verifier_a,
    )["ok"]
    clock.advance(61)
    assert service.recover_lease(
        operation_id, _admin("admin-guidance"), reason="original run ended"
    )["ok"]

    verifier_b = _agent("codex", "verifier-run-guidance-b")
    blocked = service.execute_agent(
        "reject", _reject_large_args(operation_id), principal=verifier_b,
    )
    assert blocked["errors"][0]["rule"] == "service_lease_claim_forbidden"
    assert blocked["allowed_actions"] == ["safe-reclaim"]
    assert blocked["data"]["legal_next_actions"] == ["safe-reclaim"]
    blocked_action = blocked["data"]["agent_action"]
    assert blocked_action["command"] == "safe-reclaim"
    assert blocked_action["arguments"]["submission_id"] == operation_id
    assert blocked_action["arguments"]["agent"] == "codex"

    inspected = service.execute_admin(
        "inspect", {"submission_id": operation_id}, principal=_admin("admin-inspect")
    )
    assert inspected["ok"], inspected
    assert inspected["data"]["human_actions"] == []
    inspect_actions = inspected["data"]["agent_actions_now"]
    assert len(inspect_actions) == 1
    inspect_action = inspect_actions[0]
    assert inspect_action["command"] == "safe-reclaim"
    assert inspect_action["arguments"]["submission_id"] == operation_id
    assert (
        inspect_action["arguments"]["lease_id"]
        == blocked_action["arguments"]["lease_id"]
    )


def test_fresh_verifier_on_clean_expired_other_run_is_told_to_safe_reclaim_not_admin(tmp_path):
    clock = Clock()
    service, _backend = _service(tmp_path, clock=clock, ttl=60)
    constructor = _agent("gpt", "constructor-safe-reclaim-guidance")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=constructor,
    )
    operation_id = started["submission_id"]
    assert service.execute_agent("prepare", _prepare_args(operation_id), principal=constructor)["ok"]

    old = _agent("codex", "old-verifier-safe-reclaim-guidance")
    assert service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification", "independence_attestation": "independent"},
        principal=old,
    )["ok"]
    clock.advance(61)

    fresh = _agent("codex", "fresh-verifier-safe-reclaim-guidance")
    blocked = service.execute_agent(
        "reject", _reject_large_args(operation_id), principal=fresh,
    )
    assert blocked["code"] in {"CONFLICT", "AGENT_MISMATCH"}
    assert blocked["allowed_actions"] == ["safe-reclaim"]
    assert blocked["data"]["service_access"]["state"] == "safe_reclaim_available"
    assert blocked["data"]["agent_action"]["command"] == "safe-reclaim"
    assert blocked["data"]["legal_next_actions"] == ["safe-reclaim"]
    assert "required_admin_action" not in blocked["data"]["service_access"]
    assert "no Marco/admin recovery is required" in blocked["data"]["legal_next_step"]
