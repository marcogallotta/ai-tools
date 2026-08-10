from __future__ import annotations

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
from tests.test_admin_task_target_resolution import (
    _NUMERIC_TASK_GID,
    _numeric_task_source,
)


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
    assert result["data"]["fenced_invocation"]["authority_state"] == "retired"
    released = conn.execute(
        "SELECT released_at,release_reason FROM service_leases WHERE lease_id=?",
        (lease["lease_id"],),
    ).fetchone()
    assert released["released_at"] is not None
    assert released["release_reason"].startswith("Marco kill/replace:")
    successor = result["data"]["abandonment"]["successor_operation_id"]
    assert successor
    assert successor != operation["operation_id"]




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
    assert result["data"]["fenced_invocation"]["run_id"] == lease["run_id"]
    assert result["data"]["fenced_invocation"]["authority_state"] == "retired"
    assert result["data"].get("abandonment") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM abandonment_attempts WHERE source_operation_id=?",
        (operation["operation_id"],),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()["released_at"] is not None



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
        conn, operation_id=operation["operation_id"], command="prepare"
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

    def fail_after_retirement(*args, **kwargs):
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "injected after kill retirement",
            rule="test_kill_post_retirement_failure",
        )

    monkeypatch.setattr(admin_module, "_command_abandon_operation", fail_after_retirement)
    result = app.execute("kill", dish=_dish_id(), reason="replace dead conversation")
    assert not result["ok"]

    retired = conn.execute(
        "SELECT released_at,release_reason FROM service_leases WHERE operation_id=? ORDER BY actor_attempt_seq DESC LIMIT 1",
        (operation["operation_id"],),
    ).fetchone()
    assert retired["released_at"] is not None
    assert retired["release_reason"].startswith("Marco kill/replace:")

    try:
        LeaseManager(conn).acquire(operation["operation_id"], old)
    except DishRuleError as exc:
        assert exc.rule == "killed_run_reacquire_forbidden"
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
        assert exc.rule == "killed_run_reacquire_forbidden"
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
    assert blocked_apply["errors"][0]["rule"] == "service_lease_claim_forbidden"
    assert backend.writes == writes_before_retry
    proposal_after_retry = app.conn.execute(
        "SELECT status,claimed_run_id FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal_after_retry["status"] == "approved"
    assert proposal_after_retry["claimed_run_id"] is None



def test_kill_retirement_survives_selected_lease_release_before_writer_lock(monkeypatch) -> None:
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

    retirement = conn.execute(
        """SELECT * FROM operation_run_retirements
             WHERE operation_id=? AND owner_id=? AND run_id=?""",
        (operation["operation_id"], old.owner_id, old.run_id),
    ).fetchone()
    assert retirement is not None
    assert retirement["source_lease_id"] == selected["lease_id"]

    try:
        LeaseManager(conn).acquire(operation["operation_id"], old)
    except DishRuleError as exc:
        assert exc.rule == "killed_run_reacquire_forbidden"
    else:
        raise AssertionError("run released before kill lock reacquired after retirement")


def test_kill_claimed_proposal_without_active_lease_retires_exact_run_and_preserves_successor(tmp_path) -> None:
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
    lease = LeaseManager(app.conn).acquire(
        operation_id, old, context_cycle_id=approved["cycle_id"]
    )
    claim_semantic_proposal(
        app.conn,
        proposal_id=proposal_id,
        agent="codex",
        run_id=old.run_id,
        request_id=None,
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
    retirement = app.conn.execute(
        """SELECT * FROM operation_run_retirements
             WHERE operation_id=? AND owner_id=? AND run_id=?""",
        (operation_id, old.owner_id, old.run_id),
    ).fetchone()
    assert retirement is not None
    assert retirement["source_lease_id"] == lease["lease_id"]

    try:
        LeaseManager(app.conn).acquire(
            operation_id, old, context_cycle_id=approved["cycle_id"]
        )
    except DishRuleError as exc:
        assert exc.rule == "killed_run_reacquire_forbidden"
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
    assert blocked_apply["errors"][0]["rule"] == "service_lease_claim_forbidden"
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
