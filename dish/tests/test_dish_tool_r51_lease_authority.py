from __future__ import annotations

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.database import initialize_database
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_r43_service_leases import Clock
from tests.test_dish_tool_step7_verification import Backend, TASK


def _principal(owner: str, run: str) -> ServicePrincipal:
    return ServicePrincipal(owner_id=owner, run_id=run)


def _service(tmp_path, *, clock=None, ttl=60):
    honest = tmp_path / "honest"
    honest.mkdir()
    backend = Backend()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            lease_ttl_seconds=ttl,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
        lease_now=None if clock is None else clock.now,
    )
    return service, backend


def _start(service, principal):
    result = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
    )
    assert result["ok"]
    return result


def test_inspect_actions_are_principal_aware_and_read_only(tmp_path):
    service, _backend = _service(tmp_path)
    owner = _principal("action", "owner-run")
    other = _principal("action", "other-run")
    started = _start(service, owner)
    operation_id = started["submission_id"]

    owner_view = service.execute_agent(
        "inspect", {"agent": "gpt", "submission_id": operation_id}, principal=owner
    )
    other_view = service.execute_agent(
        "inspect", {"agent": "gpt", "submission_id": operation_id}, principal=other
    )

    assert "prepare" in owner_view["allowed_actions"]
    assert owner_view["data"]["service_access"]["state"] == "owned"
    assert other_view["allowed_actions"] == []
    assert other_view["data"]["service_access"]["state"] == "held_by_other_run"

    conn = initialize_database(service.config.db_path)
    try:
        lease = LeaseManager(conn).active_for_operation(operation_id)
        assert lease["owner_id"] == owner.owner_id
        assert lease["run_id"] == owner.run_id
        assert lease["released_at"] is None
    finally:
        conn.close()


def test_expired_recovery_releases_admin_and_original_run_may_reclaim(tmp_path):
    clock = Clock()
    service, backend = _service(tmp_path, clock=clock, ttl=30)
    owner = _principal("action", "constructor-run")
    started = _start(service, owner)
    operation_id = started["submission_id"]
    clock.advance(31)

    recovered = service.recover_lease(
        operation_id, _principal("admin", "admin-run"), reason="caller ended"
    )
    assert recovered["ok"]
    assert recovered["data"]["service_lease"] is None

    forbidden = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=_principal("action", "different-run"),
    )
    assert forbidden["code"] == "AGENT_MISMATCH"
    assert forbidden["errors"][0]["rule"] == "service_lease_claim_forbidden"
    assert backend.writes == 0

    resumed = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=owner,
    )
    assert resumed["ok"]
    assert resumed["data"]["service_lease"] is None


def test_authorization_does_not_take_or_replace_live_actor_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner = _principal("action", "constructor-run")
    started = _start(service, owner)
    operation_id = started["submission_id"]

    result = service.execute_admin(
        "authorize-governed-change",
        {
            "submission_id": operation_id,
            "field": "Locks",
            "before": "Keep crisp",
            "after": "Keep very crisp",
            "reason": "Marco authorised stronger crispness",
            "run_id": "marco-run",
        },
        principal=_principal("admin", "marco-run"),
    )
    assert result["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        lease = LeaseManager(conn).active_for_operation(operation_id)
        assert lease["owner_id"] == owner.owner_id
        assert lease["run_id"] == owner.run_id
    finally:
        conn.close()


def test_terminal_operation_cannot_receive_new_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner = _principal("action", "constructor-run")
    started = _start(service, owner)
    operation_id = started["submission_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            "UPDATE operations SET status='cancelled', phase='terminal', completed_at='now', "
            "terminal_outcome='cancelled' WHERE operation_id=?",
            (operation_id,),
        )
        conn.execute(
            "UPDATE service_leases SET released_at='now', release_reason='test' "
            "WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        )
        try:
            LeaseManager(conn).acquire(operation_id, owner)
        except Exception as exc:
            assert getattr(exc, "rule", None) == "service_lease_operation_not_open"
        else:
            raise AssertionError("terminal operation accepted a new lease")
    finally:
        conn.close()


def test_admin_hold_resolution_uses_ephemeral_lease_and_hands_back_to_verification(tmp_path):
    service, _backend = _service(tmp_path)
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
        {"agent": "codex", "task_gid": "t", "kind": "verification"},
        principal=verifier,
    )
    assert reviewed["ok"]
    held = service.execute_agent(
        "reject",
        {
            "agent": "codex",
            "submission_id": operation_id,
            "route": "evidence",
            "reason": "confirm source",
            "resume_status": "pending-verification",
        },
        principal=verifier,
    )
    assert held["ok"]
    assert held["data"]["service_lease"] is None

    resolved = service.execute_admin(
        "supply-evidence",
        {
            "submission_id": operation_id,
            "detail": "Marco confirmed source",
            "resume_status": "pending-verification",
        },
        principal=_principal("admin", "marco-run"),
    )
    assert resolved["ok"]
    assert resolved["data"]["service_lease"] is None

    conn = initialize_database(service.config.db_path)
    try:
        assert LeaseManager(conn).active_for_operation(operation_id) is None
    finally:
        conn.close()

    next_verifier = _principal("action", "next-verifier-run")
    review2 = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification"},
        principal=next_verifier,
    )
    assert review2["ok"]
    assert review2["data"]["service_lease"]["run_id"] == "next-verifier-run"


def test_admin_operation_error_preserves_task_and_submission_ids(tmp_path):
    service, _backend = _service(tmp_path)
    owner = _principal("action", "constructor-run")
    started = _start(service, owner)
    operation_id = started["submission_id"]

    result = service.execute_admin(
        "recover",
        {
            "submission_id": operation_id,
            "reason": "premature recovery",
        },
        principal=_principal("admin", "marco-run"),
    )

    assert result["code"] == "AGENT_MISMATCH"
    assert result["task_gid"] == "t"
    assert result["submission_id"] == operation_id


def test_failed_repeat_verification_start_does_not_release_existing_lease(tmp_path):
    service, _backend = _service(tmp_path)
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
    first = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification"},
        principal=verifier,
    )
    assert first["ok"]

    repeated = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification"},
        principal=verifier,
    )
    assert not repeated["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        lease = LeaseManager(conn).active_for_operation(operation_id)
        assert lease is not None
        assert lease["owner_id"] == verifier.owner_id
        assert lease["run_id"] == verifier.run_id
        assert lease["released_at"] is None
    finally:
        conn.close()
