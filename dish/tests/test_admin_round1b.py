from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dish_service.admin_cli import build_parser
from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.errors import DishRuleError
from dish_tool.admin import DishAdminApplication
from dish_tool.admin_human import render_admin_result
from dish_tool.database import confirm_task_content
from dish_tool.database_initialization import initialize_database
from dish_tool.identifiers import stable_dish_uuid_for_asana_identity
from dish_tool.operation_execution import claim_operation_execution
from dish_tool.semantic_proposals import approve_semantic_proposal, claim_semantic_proposal
from tests.support.abandonment import Backend
from tests.support.semantic_proposal_bundle_workflow import _approved_service_proposal_runtime
from tests.support.abandonment_admin import _released_actor_lease
from tests.support.verification import TASK, make_app, review_and_inspect
from tests.support.abandonment import _NUMERIC_TASK_GID, _numeric_task_source


def _dish_id() -> str:
    return str(stable_dish_uuid_for_asana_identity("task", _NUMERIC_TASK_GID))


def _dish_url(slug: str = "decorative-title") -> str:
    return f"https://dish.example/dishes/{_dish_id()}/{slug}"


def test_inspect_resting_dish_by_frontend_url_uses_uuid_not_slug() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID, title="Exact Dish")
    confirm_task_content(
        conn,
        task_gid=_NUMERIC_TASK_GID,
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
        boundary="test-baseline",
    )
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute("inspect", dish=_dish_url("completely-wrong-old-slug"))

    assert result["ok"], result
    assert result["state"] == "resting"
    assert result["submission_id"] is None
    assert result["task_gid"] == _NUMERIC_TASK_GID
    assert result["data"]["dish_id"] == _dish_id()
    assert result["data"]["task_title"] == "Exact Dish"
    assert result["data"]["outstanding_invocation"] is None


def test_inspect_active_dish_exposes_high_level_run_and_replace_choice() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    LeaseManager(conn).acquire(
        operation["operation_id"], ServicePrincipal("owner", "live-run")
    )
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute("inspect", dish=_dish_id())

    assert result["ok"], result
    invocation = result["data"]["outstanding_invocation"]
    assert invocation["run_id"] == "live-run"
    assert invocation["authority_state"] == "active"
    assert invocation["replacement_required"] is True
    actions = result["data"]["human_actions"]
    assert [action["command"] for action in actions] == ["kill"]
    assert actions[0]["arguments"]["positional"] == [_dish_id()]
    assert "expire-lease" not in actions[0]["shell_command"]
    assert "abandon-operation" not in actions[0]["shell_command"]


def test_inspect_clean_expired_lease_does_not_imply_original_run_was_evicted() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    lease = LeaseManager(
        conn,
        ttl_seconds=30,
        now=lambda: datetime(2020, 1, 1, tzinfo=timezone.utc),
    ).acquire(operation["operation_id"], ServicePrincipal("owner", "returning-run"))
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute("inspect", dish=_dish_id())

    assert result["ok"], result
    assert "original run to resume" in result["data"]["waiting_for"]
    assert "original owner/run may still resume" in result["data"]["problem"]
    assert any(
        isinstance(action, dict) and action.get("command") == "safe-reclaim"
        for action in result["data"]["agent_actions_now"]
    )


def test_kill_frontend_url_fences_run_and_prepares_safe_successor() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    lease = LeaseManager(conn).acquire(
        operation["operation_id"], ServicePrincipal("owner", "dead-run")
    )
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute(
        "kill", dish=_dish_url("ignored-slug"), reason="the conversation is gone"
    )

    assert result["ok"], result
    assert result["data"]["outcome"] == "replacement_ready"
    assert result["data"]["dish_id"] == _dish_id()
    assert result["data"]["fenced_invocation"]["run_id"] == "dead-run"
    assert result["data"]["fenced_invocation"]["authority_state"] == "revoked"
    released = conn.execute(
        "SELECT released_at,release_reason FROM service_leases WHERE lease_id=?",
        (lease["lease_id"],),
    ).fetchone()
    assert released["released_at"] is not None
    assert released["release_reason"].startswith("Marco kill/replace:")
    successor = result["data"]["abandonment"]["successor_operation_id"]
    assert successor
    assert successor != operation["operation_id"]

    continuation = app.execute("inspect", dish=_dish_id())
    assert continuation["ok"], continuation
    assert continuation["data"]["waiting_for"] != "terminal"
    assert any(
        isinstance(action, dict) and action.get("command") == "start"
        for action in continuation["data"]["agent_actions_now"]
    )




def test_kill_inactive_safe_reclaimable_run_does_not_invent_abandonment() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    lease = _released_actor_lease(conn, operation["operation_id"])
    app = DishAdminApplication(conn, backend=backend)

    inspected = app.execute("inspect", dish=_dish_id())
    assert inspected["ok"], inspected
    assert inspected["data"]["human_actions"] == []
    assert any(
        isinstance(action, dict) and action.get("command") == "safe-reclaim"
        for action in inspected["data"]["agent_actions_now"]
    )

    result = app.execute("kill", dish=_dish_id(), reason="replace the old run")

    assert result["ok"], result
    assert result["data"]["outcome"] == "replacement_ready"
    fenced = result["data"]["fenced_invocation"]
    assert fenced["owner_id"] == lease["owner_id"]
    assert fenced["run_id"] == lease["run_id"]
    assert fenced["lease_id"] == lease["lease_id"]
    assert fenced["authority_source"] == "historical_actor_lease"
    assert fenced["authority_state"] == "revoked"
    assert result["data"].get("abandonment") is None
    revocation = conn.execute(
        """SELECT * FROM operation_run_revocations
             WHERE operation_id=? AND owner_id=? AND run_id=?""",
        (operation["operation_id"], lease["owner_id"], lease["run_id"]),
    ).fetchone()
    assert revocation is not None
    assert revocation["source_lease_id"] == lease["lease_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM abandonment_attempts WHERE source_operation_id=?",
        (operation["operation_id"],),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()["released_at"] is not None

    old = ServicePrincipal(str(lease["owner_id"]), str(lease["run_id"]))
    with pytest.raises(DishRuleError) as excinfo:
        LeaseManager(conn).acquire(operation["operation_id"], old)
    assert excinfo.value.rule == "killed_run_revoked"
    with pytest.raises(DishRuleError) as excinfo:
        claim_operation_execution(
            conn,
            operation_id=operation["operation_id"],
            command="prepare",
            owner_id=old.owner_id,
            run_id=old.run_id,
        )
    assert excinfo.value.rule == "killed_run_revoked"



def test_kill_preserves_pending_semantic_proposal_checkpoint(tmp_path) -> None:
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Linked governed correction",
        file_path=str(candidate),
        run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=app.release_loader
    )

    result = admin.execute(
        "kill", dish=operation_id, reason="replace the unavailable conversation"
    )

    assert result["ok"], result
    assert result["data"]["outcome"] == "checkpoint_preserved"
    proposal = app.conn.execute(
        "SELECT status,reviewed_at FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal["status"] == "pending"
    assert proposal["reviewed_at"] is None


def test_kill_resting_dish_is_idempotent_noop() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    confirm_task_content(
        conn,
        task_gid=_NUMERIC_TASK_GID,
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
        boundary="test-baseline",
    )
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute("kill", dish=_dish_id(), reason="nothing should be running")

    assert result["ok"], result
    assert result["data"]["outcome"] == "no_outstanding_invocation"
    assert result["state"] == "resting"


def test_kill_fails_closed_while_workflow_mutation_is_actively_committing() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    lease = LeaseManager(conn).acquire(
        operation["operation_id"], ServicePrincipal("owner", "live-run")
    )
    claim_operation_execution(
        conn,
        operation_id=operation["operation_id"],
        command="prepare",
        owner_id="owner",
        run_id="live-run",
    )
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute("kill", dish=_dish_id(), reason="replace it")

    assert not result["ok"]
    assert result["code"] == "CONFLICT"
    assert any(error.get("rule") == "kill_mutation_in_progress" for error in result["errors"])
    row = conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()
    assert row["released_at"] is None


def test_admin_cli_calls_the_target_dish_and_accepts_frontend_url() -> None:
    parser = build_parser()
    inspect = vars(parser.parse_args(["inspect", _dish_url("old-title")]))
    kill = vars(parser.parse_args(["kill", _dish_url("new-title"), "--reason", "gone"]))

    assert inspect["dish"] == _dish_url("old-title")
    assert "submission_id" not in inspect
    assert kill["dish"] == _dish_url("new-title")
    assert kill["reason"] == "gone"


def test_human_inspect_and_kill_render_consequence_before_internal_mechanics() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID, title="Exact Dish")
    operation = _numeric_task_source(conn, backend)
    LeaseManager(conn).acquire(
        operation["operation_id"], ServicePrincipal("owner", "dead-run")
    )
    app = DishAdminApplication(conn, backend=backend)

    inspected = app.execute("inspect", dish=_dish_id())
    inspect_text = render_admin_result(inspected, profile="test")
    assert "Dish: Exact Dish" in inspect_text
    assert "Outstanding run: dead-run" in inspect_text
    assert "leave this run alone, or replace it" in inspect_text
    assert "dish-admin kill" in inspect_text
    assert "expire-lease" not in inspect_text

    killed = app.execute("kill", dish=_dish_id(), reason="conversation gone")
    kill_text = render_admin_result(killed, profile="test")
    consequence = killed["data"]["human_consequence"]
    assert consequence in kill_text
    assert kill_text.index(consequence) < kill_text.find("Current Dish state:") if "Current Dish state:" in kill_text else True


def test_kill_release_is_durable_revocation_before_abandonment_finishes(monkeypatch) -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    old = ServicePrincipal("owner", "dead-run")
    LeaseManager(conn).acquire(operation["operation_id"], old)
    app = DishAdminApplication(conn, backend=backend)

    import dish_tool.admin as admin_module

    def fail_after_revocation(*args, **kwargs):
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "injected after kill revocation",
            rule="test_kill_post_revocation_failure",
        )

    monkeypatch.setattr(admin_module, "_command_abandon_operation", fail_after_revocation)
    result = app.execute("kill", dish=_dish_id(), reason="replace dead conversation")
    assert not result["ok"]

    released = conn.execute(
        "SELECT released_at,release_reason FROM service_leases WHERE operation_id=? ORDER BY actor_attempt_seq DESC LIMIT 1",
        (operation["operation_id"],),
    ).fetchone()
    assert released["released_at"] is not None
    assert released["release_reason"].startswith("Marco kill/replace:")

    try:
        LeaseManager(conn).acquire(operation["operation_id"], old)
    except DishRuleError as exc:
        assert exc.rule == "killed_run_revoked"
    else:
        raise AssertionError("killed run reacquired the source operation")


def test_kill_preserved_claimed_proposal_cannot_be_reclaimed_by_killed_run(tmp_path) -> None:
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal-killed.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Linked governed correction",
        file_path=str(candidate),
        run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    approved = approve_semantic_proposal(
        app.conn,
        proposal_id=proposal_id,
        live_title=backend.title,
        live_notes=backend.notes,
        reason="Marco approved exact proposal",
    )
    assert approved["status"] == "approved"

    old = ServicePrincipal("action", "proposal-applier")
    LeaseManager(app.conn).acquire(operation_id, old, context_cycle_id=approved["cycle_id"])
    claim_semantic_proposal(
        app.conn,
        proposal_id=proposal_id,
        agent="codex",
        run_id=old.run_id,
        request_id=None,
        owner_id=old.owner_id,
    )
    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=app.release_loader
    )
    killed = admin.execute("kill", dish=operation_id, reason="replace proposal applier")
    assert killed["ok"], killed
    assert killed["data"]["outcome"] == "checkpoint_preserved"

    proposal = app.conn.execute(
        "SELECT status,claimed_run_id FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal["status"] == "approved"
    assert proposal["claimed_run_id"] is None

    try:
        LeaseManager(app.conn).acquire(operation_id, old, context_cycle_id=approved["cycle_id"])
    except DishRuleError as exc:
        assert exc.rule == "killed_run_revoked"
    else:
        raise AssertionError("killed proposal applier reacquired the source operation")

    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "dish.db",
            honest_root=tmp_path / "honest",
            backup_dir=tmp_path / "managed-backups",
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
            port=0,
        ),
        backend_factory=lambda: backend,
        release_loader=app.release_loader,
    )
    writes_before_retry = backend.writes
    import uuid
    blocked_apply = service.execute_agent(
        "apply-proposal",
        {
            "proposal_id": proposal_id,
            "agent": "codex",
            "model": "gpt-5.6-sol",
        },
        principal=old,
        request_id=str(uuid.uuid4()),
    )
    assert not blocked_apply["ok"]
    assert blocked_apply["errors"][0]["rule"] == "killed_run_revoked"
    assert backend.writes == writes_before_retry
    proposal_after_retry = app.conn.execute(
        "SELECT status,claimed_run_id FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal_after_retry["status"] == "approved"
    assert proposal_after_retry["claimed_run_id"] is None


def test_revoked_run_inspect_does_not_advertise_approved_proposal_application(tmp_path) -> None:
    service, _backend, proposal_id, _task_gid = _approved_service_proposal_runtime(
        tmp_path
    )
    principal = ServicePrincipal("action", "killed-proposal-inspector")
    conn = initialize_database(service.config.db_path)
    try:
        proposal = conn.execute(
            "SELECT operation_id FROM semantic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        operation_id = str(proposal["operation_id"])
    finally:
        conn.close()

    import uuid

    before = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=principal,
        request_id=str(uuid.uuid4()),
    )
    assert "apply-proposal" in before["allowed_actions"]

    conn = initialize_database(service.config.db_path)
    try:
        from dish_tool.database import revoke_operation_run_in_transaction
        from dish_tool.transactions import immediate_transaction

        with immediate_transaction(conn, "test_revoke_proposal_inspector"):
            revoke_operation_run_in_transaction(
                conn,
                operation_id=operation_id,
                owner_id=principal.owner_id,
                run_id=principal.run_id,
                reason="test explicit kill",
            )
    finally:
        conn.close()

    after = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=principal,
        request_id=str(uuid.uuid4()),
    )
    assert after["allowed_actions"] == []
    assert after["data"]["service_access"]["state"] == "revoked"
    assert after["data"]["service_access"]["rule"] == "killed_run_revoked"



def test_kill_revocation_survives_selected_lease_release_before_writer_lock(monkeypatch) -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    old = ServicePrincipal("owner", "racy-dead-run")
    selected = LeaseManager(conn).acquire(operation["operation_id"], old)
    app = DishAdminApplication(conn, backend=backend)

    # _command_kill imports the function locally, so replace the source module function.
    import dish_tool.operation_execution as execution_module
    original = execution_module.live_operation_execution_claim
    calls = {"count": 0}

    def release_between_resolution_and_lock(conn_arg, *, operation_id):
        calls["count"] += 1
        if calls["count"] == 1:
            LeaseManager(conn_arg).release(
                operation_id, old, reason="normal release raced kill"
            )
        return original(conn_arg, operation_id=operation_id)

    monkeypatch.setattr(
        execution_module, "live_operation_execution_claim", release_between_resolution_and_lock
    )
    # The local import occurs when kill executes, so the patched source is what it receives.
    killed = app.execute("kill", dish=_dish_id(), reason="replace raced run")
    assert killed["ok"], killed
    assert killed["data"]["fenced_invocation"]["run_id"] == old.run_id

    revocation = conn.execute(
        """SELECT * FROM operation_run_revocations
             WHERE operation_id=? AND owner_id=? AND run_id=?""",
        (operation["operation_id"], old.owner_id, old.run_id),
    ).fetchone()
    assert revocation is not None
    assert revocation["source_lease_id"] == selected["lease_id"]

    try:
        LeaseManager(conn).acquire(operation["operation_id"], old)
    except DishRuleError as exc:
        assert exc.rule == "killed_run_revoked"
    else:
        raise AssertionError("run released before kill lock reacquired after revocation")


def test_kill_claimed_proposal_without_active_lease_revokes_exact_run_and_preserves_successor(tmp_path) -> None:
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author-no-lease")
    candidate = tmp_path / "proposal-killed-no-lease.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Linked governed correction",
        file_path=str(candidate),
        run_id="proposal-author-no-lease",
    )
    proposal_id = queued["data"]["proposal_id"]
    approved = approve_semantic_proposal(
        app.conn,
        proposal_id=proposal_id,
        live_title=backend.title,
        live_notes=backend.notes,
        reason="Marco approved exact proposal",
    )
    old = ServicePrincipal("action", "proposal-applier-no-lease")
    LeaseManager(app.conn).acquire(
        operation_id, old, context_cycle_id=approved["cycle_id"]
    )
    claim_semantic_proposal(
        app.conn,
        proposal_id=proposal_id,
        agent="codex",
        run_id=old.run_id,
        request_id=None,
        owner_id=old.owner_id,
    )
    LeaseManager(app.conn).release(
        operation_id, old, reason="proposal applier conversation ended"
    )
    assert LeaseManager(app.conn).active_for_operation(operation_id) is None

    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=app.release_loader
    )
    killed = admin.execute("kill", dish=operation_id, reason="replace proposal applier")
    assert killed["ok"], killed
    assert killed["data"]["outcome"] == "checkpoint_preserved"
    assert killed["data"]["fenced_invocation"]["owner_id"] == old.owner_id
    assert killed["data"]["fenced_invocation"]["run_id"] == old.run_id

    proposal = app.conn.execute(
        "SELECT status,claimed_run_id FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal["status"] == "approved"
    assert proposal["claimed_run_id"] is None
    revocation = app.conn.execute(
        """SELECT * FROM operation_run_revocations
             WHERE operation_id=? AND owner_id=? AND run_id=?""",
        (operation_id, old.owner_id, old.run_id),
    ).fetchone()
    assert revocation is not None
    assert revocation["source_lease_id"] is not None
    source = app.conn.execute(
        "SELECT owner_id,run_id,released_at FROM service_leases WHERE lease_id=?",
        (revocation["source_lease_id"],),
    ).fetchone()
    assert (source["owner_id"], source["run_id"]) == (old.owner_id, old.run_id)
    assert source["released_at"] is not None

    try:
        LeaseManager(app.conn).acquire(
            operation_id, old, context_cycle_id=approved["cycle_id"]
        )
    except DishRuleError as exc:
        assert exc.rule == "killed_run_revoked"
    else:
        raise AssertionError("killed proposal applier reacquired without an active lease")

    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "dish.db",
            honest_root=tmp_path / "honest",
            backup_dir=tmp_path / "managed-backups",
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
            port=0,
        ),
        backend_factory=lambda: backend,
        release_loader=app.release_loader,
    )
    writes_before_retry = backend.writes
    import uuid
    blocked_apply = service.execute_agent(
        "apply-proposal",
        {
            "proposal_id": proposal_id,
            "agent": "codex",
            "model": "gpt-5.6-sol",
        },
        principal=old,
        request_id=str(uuid.uuid4()),
    )
    assert not blocked_apply["ok"]
    assert blocked_apply["errors"][0]["rule"] == "killed_run_revoked"
    assert backend.writes == writes_before_retry
    proposal_after_retry = app.conn.execute(
        "SELECT status,claimed_run_id FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal_after_retry["status"] == "approved"
    assert proposal_after_retry["claimed_run_id"] is None

    successor = ServicePrincipal("action", "legitimate-successor")
    successor_lease = LeaseManager(app.conn).acquire(
        operation_id, successor, context_cycle_id=approved["cycle_id"]
    )
    assert successor_lease["run_id"] == successor.run_id


def test_killed_run_cannot_renew_even_if_a_matching_lease_row_remains(tmp_path) -> None:
    app, backend, operation_id, _ = make_app(tmp_path)
    old = ServicePrincipal("action", "renew-killed-run")
    lease = LeaseManager(app.conn).acquire(operation_id, old)
    from dish_tool.database import revoke_operation_run_in_transaction
    from dish_tool.transactions import immediate_transaction

    with immediate_transaction(app.conn, "test_revoke_without_lease_cleanup"):
        revoke_operation_run_in_transaction(
            app.conn,
            operation_id=operation_id,
            owner_id=old.owner_id,
            run_id=old.run_id,
            source_lease_id=lease["lease_id"],
            reason="test exact revocation",
        )

    try:
        LeaseManager(app.conn).renew(operation_id, old)
    except DishRuleError as exc:
        assert exc.rule == "killed_run_revoked"
        assert str(exc) == "This Dish run has been killed."
    else:
        raise AssertionError("killed run renewed authority")


def test_normal_non_kill_lease_release_allows_same_run_to_reacquire(tmp_path) -> None:
    app, _backend, operation_id, _ = make_app(tmp_path)
    principal = ServicePrincipal("action", "normal-reacquire")
    leases = LeaseManager(app.conn)
    first = leases.acquire(operation_id, principal)
    leases.admin_expire_selected(first["lease_id"], reason="ordinary process expiry")

    second = leases.acquire(operation_id, principal)
    assert second["run_id"] == principal.run_id
    assert second["owner_id"] == principal.owner_id
    assert second["lease_id"] != first["lease_id"]


def test_inspect_reports_explicit_revocation_even_before_lease_cleanup() -> None:
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    principal = ServicePrincipal("owner", "revoked-before-cleanup")
    lease = LeaseManager(conn).acquire(operation["operation_id"], principal)
    from dish_tool.database import revoke_operation_run_in_transaction
    from dish_tool.transactions import immediate_transaction

    with immediate_transaction(conn, "test_explicit_revocation_visibility"):
        revoke_operation_run_in_transaction(
            conn,
            operation_id=operation["operation_id"],
            owner_id=principal.owner_id,
            run_id=principal.run_id,
            source_lease_id=lease["lease_id"],
            reason="test explicit revocation",
        )

    result = DishAdminApplication(conn, backend=backend).execute(
        "inspect", dish=_dish_id()
    )

    assert result["ok"], result
    assert result["data"]["outstanding_invocation"]["run_id"] == principal.run_id
    assert result["data"]["outstanding_invocation"]["authority_state"] == "revoked"


def test_kill_mechanical_claim_then_review_approve_cannot_resurrect_exact_run(tmp_path) -> None:
    import dish_tool.admin as admin_module
    from dish_tool.constants import MECHANICAL_PROPOSAL_AGENT

    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="mechanical-kill-author")
    candidate = tmp_path / "mechanical-kill-proposal.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Linked governed correction",
        file_path=str(candidate),
        run_id="mechanical-kill-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    approved = approve_semantic_proposal(
        app.conn,
        proposal_id=proposal_id,
        live_title=backend.title,
        live_notes=backend.notes,
        reason="Marco approved exact proposal",
    )
    mechanical_run = admin_module._mechanical_proposal_run_id(proposal_id)
    claim_semantic_proposal(
        app.conn,
        proposal_id=proposal_id,
        agent=MECHANICAL_PROPOSAL_AGENT,
        owner_id="dish-mechanical",
        run_id=mechanical_run,
        request_id=None,
    )
    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=app.release_loader
    )

    killed = admin.execute(
        "kill", dish=operation_id, reason="replace killed mechanical proposal application"
    )
    assert killed["ok"], killed
    assert killed["data"]["outcome"] == "checkpoint_preserved"
    assert killed["data"]["fenced_invocation"]["owner_id"] == "dish-mechanical"
    assert killed["data"]["fenced_invocation"]["run_id"] == mechanical_run
    assert killed["data"]["fenced_invocation"]["proposal_id"] == proposal_id

    writes_before = backend.writes
    retried = admin.execute("review-approve", proposal_id=proposal_id)
    assert not retried["ok"]
    assert retried["errors"][0]["rule"] == "killed_run_revoked"
    assert backend.writes == writes_before
    proposal = app.conn.execute(
        """SELECT status,claimed_owner_id,claimed_run_id,applied_at
             FROM semantic_proposals WHERE proposal_id=?""",
        (proposal_id,),
    ).fetchone()
    assert proposal["status"] == "approved"
    assert proposal["claimed_owner_id"] is None
    assert proposal["claimed_run_id"] is None
    assert proposal["applied_at"] is None


def test_kill_loses_closed_when_mechanical_application_holds_canonical_mutation_claim(
    tmp_path,
) -> None:
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="mechanical-race-author")
    candidate = tmp_path / "mechanical-race-proposal.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Linked governed correction",
        file_path=str(candidate),
        run_id="mechanical-race-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=app.release_loader
    )
    kill_results: list[dict] = []

    def try_kill_during_write(**_kwargs) -> None:
        kill_results.append(
            admin.execute(
                "kill",
                dish=operation_id,
                reason="race with consequential proposal write",
            )
        )

    backend.before("update_task_content", try_kill_during_write)
    writes_before = backend.writes
    applied = admin.execute(
        "review-approve",
        proposal_id=proposal_id,
        reason="Marco approves this exact linked bundle.",
    )

    assert applied["ok"], applied
    assert backend.writes == writes_before + 1
    assert len(kill_results) == 1
    blocked_kill = kill_results[0]
    assert not blocked_kill["ok"]
    assert blocked_kill["errors"][0]["rule"] == "kill_mutation_in_progress"
    assert app.conn.execute(
        "SELECT COUNT(*) FROM operation_run_revocations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 0


def test_kill_request_replay_uses_durable_exact_binding_after_revocation_crash(
    tmp_path, monkeypatch
) -> None:
    import uuid
    import dish_tool.admin as admin_module
    from tests.support.request_restore import SimulatedSigkill
    from tests.support.service_foundation import _release_loader

    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)
    killed_principal = ServicePrincipal("owner-before-crash", "run-before-crash")
    lease = LeaseManager(conn).acquire(source["operation_id"], killed_principal)
    conn.close()
    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=db_path,
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
            port=0,
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    admin_principal = ServicePrincipal("marco", str(uuid.uuid4()))
    request_id = str(uuid.uuid4())
    original_commit = admin_module._commit_kill_revocation

    def crash_after_revocation(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        raise SimulatedSigkill("immediately after durable kill revocation")

    monkeypatch.setattr(admin_module, "_commit_kill_revocation", crash_after_revocation)
    with pytest.raises(SimulatedSigkill):
        service.execute_admin(
            "kill",
            {"dish": _NUMERIC_TASK_GID, "reason": "replace crashed conversation"},
            principal=admin_principal,
            request_id=request_id,
        )

    conn = initialize_database(db_path)
    try:
        request = conn.execute(
            "SELECT status,operation_id FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        binding = conn.execute(
            "SELECT * FROM kill_request_bindings WHERE request_id=?", (request_id,)
        ).fetchone()
        revocation = conn.execute(
            "SELECT * FROM operation_run_revocations WHERE revocation_id=?",
            (binding["revocation_id"],),
        ).fetchone()
        assert request["status"] == "pending"
        assert request["operation_id"] is None
        assert binding["operation_id"] == source["operation_id"]
        assert binding["task_gid"] == _NUMERIC_TASK_GID
        assert binding["target_owner_id"] == killed_principal.owner_id
        assert binding["target_run_id"] == killed_principal.run_id
        assert binding["source_lease_id"] == lease["lease_id"]
        assert binding["source_lease_was_active"] == 1
        assert revocation["operation_id"] == source["operation_id"]
        assert revocation["owner_id"] == killed_principal.owner_id
        assert revocation["run_id"] == killed_principal.run_id
    finally:
        conn.close()

    monkeypatch.setattr(admin_module, "_commit_kill_revocation", original_commit)
    original_resolve = admin_module.resolve_admin_dish_target

    def forbid_high_level_reresolution(conn_arg, raw):
        assert str(raw) != _NUMERIC_TASK_GID, "replay resolved the Dish again"
        return original_resolve(conn_arg, raw)

    monkeypatch.setattr(
        admin_module, "resolve_admin_dish_target", forbid_high_level_reresolution
    )
    replayed = service.execute_admin(
        "kill",
        {"dish": _NUMERIC_TASK_GID, "reason": "replace crashed conversation"},
        principal=admin_principal,
        request_id=request_id,
    )
    assert replayed["ok"], replayed
    assert replayed["submission_id"] == source["operation_id"]
    assert replayed["data"]["fenced_invocation"]["owner_id"] == killed_principal.owner_id
    assert replayed["data"]["fenced_invocation"]["run_id"] == killed_principal.run_id

    conn = initialize_database(db_path)
    try:
        settled = conn.execute(
            "SELECT status,operation_id,task_gid,result_json FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert settled["status"] == "completed"
        assert settled["operation_id"] == source["operation_id"]
        assert settled["task_gid"] == _NUMERIC_TASK_GID
        assert settled["result_json"]
        assert conn.execute(
            """SELECT COUNT(*) FROM operation_run_revocations
                 WHERE operation_id=? AND owner_id=? AND run_id=?""",
            (source["operation_id"], killed_principal.owner_id, killed_principal.run_id),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_claimed_proposal_same_run_uuid_different_owner_has_no_claim_or_lease_authority(
    tmp_path,
) -> None:
    import uuid

    service, _backend, proposal_id, _task_gid = _approved_service_proposal_runtime(tmp_path)
    shared_run = str(uuid.uuid4())
    owner_a = ServicePrincipal("proposal-owner-a", shared_run)
    owner_b = ServicePrincipal("proposal-owner-b", shared_run)
    conn = initialize_database(service.config.db_path)
    try:
        proposal = conn.execute(
            "SELECT operation_id,cycle_id FROM semantic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        operation_id = str(proposal["operation_id"])
        LeaseManager(conn).acquire(
            operation_id, owner_a, context_cycle_id=str(proposal["cycle_id"])
        )
        claim_semantic_proposal(
            conn,
            proposal_id=proposal_id,
            agent="codex",
            owner_id=owner_a.owner_id,
            run_id=owner_a.run_id,
            request_id=None,
        )
        LeaseManager(conn).release(
            operation_id, owner_a, reason="leave claimed proposal for exact resume"
        )
    finally:
        conn.close()

    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=owner_b,
        request_id=str(uuid.uuid4()),
    )
    assert inspected["allowed_actions"] == []
    assert inspected["data"]["service_access"]["state"] == "semantic_proposal_claimed_by_other_run"
    assert inspected["data"]["service_access"]["owner_id"] == owner_a.owner_id
    assert inspected["data"]["service_access"]["run_id"] == shared_run
    assert inspected["data"]["semantic_proposal"]["claimed_owner_id"] == owner_a.owner_id

    blocked = service.execute_agent(
        "apply-proposal",
        {"proposal_id": proposal_id, "agent": "codex", "model": "gpt-5.6-sol"},
        principal=owner_b,
        request_id=str(uuid.uuid4()),
    )
    assert not blocked["ok"]
    assert blocked["errors"][0]["rule"] == "service_lease_claim_forbidden"

    conn = initialize_database(service.config.db_path)
    try:
        active = LeaseManager(conn).active_for_operation(operation_id)
        assert active is None or active["owner_id"] != owner_b.owner_id
        proposal = conn.execute(
            "SELECT status,claimed_owner_id,claimed_run_id FROM semantic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        assert proposal["status"] == "claimed"
        assert proposal["claimed_owner_id"] == owner_a.owner_id
        assert proposal["claimed_run_id"] == shared_run
        with pytest.raises(DishRuleError) as excinfo:
            claim_semantic_proposal(
                conn,
                proposal_id=proposal_id,
                agent="codex",
                owner_id=owner_b.owner_id,
                run_id=shared_run,
                request_id=None,
            )
        assert excinfo.value.rule == "semantic_proposal_claimed"
    finally:
        conn.close()
