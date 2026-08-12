from __future__ import annotations

from datetime import datetime, timezone
import uuid

import dish_tool.admin as admin_module

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.database import (
    confirm_task_content,
    create_operation,
    declare_operation_step,
    operation_run_revocation,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.models import OperationActors
from tests.support.abandonment import Backend, _source
from tests.support.abandonment_admin import _released_actor_lease


def _operation(conn, *, task_gid: str, title: str, run_id: str):
    baseline = confirm_task_content(
        conn,
        task_gid=task_gid,
        title=title,
        notes="",
        schema_version="2",
        boundary="bulk-kill-test",
    )
    return create_operation(
        conn,
        task_gid=task_gid,
        operation_kind="planning",
        expected_identity=baseline.digest,
        schema_version="2",
        expected_section_gid="pi",
        actors=OperationActors(editor_agent="gpt", run_id=run_id),
    )


def test_kill_all_expired_revokes_only_expired_unreleased_run():
    conn = initialize_database(":memory:")
    expired_op = _operation(conn, task_gid="expired", title="Expired Dish", run_id="old")
    active_op = _operation(conn, task_gid="active", title="Active Dish", run_id="live")
    old_clock = lambda: datetime(2025, 1, 1, tzinfo=timezone.utc)
    old = LeaseManager(conn, ttl_seconds=1, now=old_clock).acquire(
        expired_op["operation_id"], ServicePrincipal("owner", "old")
    )
    active = LeaseManager(conn).acquire(
        active_op["operation_id"], ServicePrincipal("owner", "live")
    )
    backend = Backend(task_gid="expired", title="Expired Dish", section="pi")

    result = DishAdminApplication(conn, backend=backend).execute(
        "kill-all-expired", reason="clear dead test runs", confirmed=True
    )

    assert result["ok"], result
    assert result["data"]["selected_count"] == 1
    assert result["data"]["killed_count"] == 1
    assert result["data"]["failed_count"] == 0
    assert operation_run_revocation(
        conn, operation_id=expired_op["operation_id"], owner_id="owner", run_id="old"
    ) is not None
    assert operation_run_revocation(
        conn, operation_id=active_op["operation_id"], owner_id="owner", run_id="live"
    ) is None
    assert conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (old["lease_id"],)
    ).fetchone()["released_at"] is not None
    assert conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (active["lease_id"],)
    ).fetchone()["released_at"] is None


def test_kill_all_revokes_active_unreleased_run():
    conn = initialize_database(":memory:")
    op = _operation(conn, task_gid="active", title="Active Dish", run_id="live")
    LeaseManager(conn).acquire(op["operation_id"], ServicePrincipal("owner", "live"))
    backend = Backend(task_gid="active", title="Active Dish", section="pi")

    result = DishAdminApplication(conn, backend=backend).execute(
        "kill-all", reason="no agents are running", confirmed=True
    )

    assert result["ok"], result
    assert result["data"]["selected_count"] == 1
    assert operation_run_revocation(
        conn, operation_id=op["operation_id"], owner_id="owner", run_id="live"
    ) is not None


def test_bulk_kill_requires_explicit_confirmation():
    conn = initialize_database(":memory:")
    op = _operation(conn, task_gid="active", title="Active Dish", run_id="live")
    lease = LeaseManager(conn).acquire(
        op["operation_id"], ServicePrincipal("owner", "live")
    )
    result = DishAdminApplication(
        conn, backend=Backend(task_gid="active", title="Active Dish", section="pi")
    ).execute("kill-all", reason="no agents are running", confirmed=False)

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "bulk_kill_confirmation_required"
    assert conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()["released_at"] is None


def test_bulk_kill_exact_precondition_does_not_kill_successor_run():
    conn = initialize_database(":memory:")
    op = _operation(conn, task_gid="dish", title="Dish", run_id="old")
    old = LeaseManager(conn).acquire(op["operation_id"], ServicePrincipal("owner", "old"))
    LeaseManager(conn).release(op["operation_id"], None, reason="old ended", admin=True)
    successor = LeaseManager(conn).acquire(
        op["operation_id"], ServicePrincipal("owner", "successor")
    )
    app = DishAdminApplication(
        conn, backend=Backend(task_gid="dish", title="Dish", section="pi")
    )

    result = app.execute(
        "kill",
        dish=str(op["operation_id"]),
        reason="bulk snapshot",
        expected_owner_id="owner",
        expected_run_id="old",
        expected_lease_id=str(old["lease_id"]),
    )

    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "bulk_kill_target_changed"
    assert operation_run_revocation(
        conn, operation_id=op["operation_id"], owner_id="owner", run_id="successor"
    ) is None
    assert conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (successor["lease_id"],)
    ).fetchone()["released_at"] is None


def test_bulk_kill_cli_flags_are_explicit():
    from dish_service.admin_cli import build_parser

    all_args = vars(build_parser().parse_args(["kill-all", "--yes"]))
    expired_args = vars(build_parser().parse_args(["kill-all-expired", "--yes"]))

    assert all_args["command"] == "kill-all"
    assert all_args["confirmed"] is True
    assert expired_args["command"] == "kill-all-expired"
    assert expired_args["confirmed"] is True


def test_automatic_abandonment_reconcile_uses_distinct_derived_request_identity():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    lease = _released_actor_lease(conn, source["operation_id"])
    declare_operation_step(conn, source["operation_id"], "unfinished", {"x": 1})
    parent_request_id = "11111111-1111-4111-8111-111111111111"
    app = DishAdminApplication(
        conn, backend=backend, invocation_request_id=parent_request_id
    )

    blocked = app.execute(
        "abandon-operation",
        submission_id=source["operation_id"],
        lease_id=lease["lease_id"],
        reason="the original conversation is permanently unavailable",
    )

    assert blocked["ok"], blocked
    abandonment_id = blocked["data"]["abandonment_id"]
    nested = admin_module._nested_admin_application(
        app, command="reconcile-abandonment", identity=abandonment_id
    )
    reconciled = admin_module._command_reconcile_abandonment(
        nested,
        trace=admin_module.AdminTrace(submission_id=source["operation_id"]),
        abandonment_id=abandonment_id,
    )

    assert reconciled["ok"], reconciled
    expected_nested_request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dish-admin-nested:{parent_request_id}:reconcile-abandonment:{abandonment_id}",
        )
    )
    executions = conn.execute(
        "SELECT request_id,command,status FROM operation_executions ORDER BY created_at"
    ).fetchall()
    assert [row["request_id"] for row in executions] == [
        parent_request_id,
        expected_nested_request_id,
    ]
    assert [row["command"] for row in executions] == [
        "abandon-operation",
        "reconcile-abandonment",
    ]
    assert all(row["status"] == "completed" for row in executions)


def test_bulk_kill_counts_committed_revocation_separately_from_downstream_error(monkeypatch):
    conn = initialize_database(":memory:")
    op = _operation(conn, task_gid="dish", title="Dish", run_id="dead")
    LeaseManager(conn).acquire(
        op["operation_id"], ServicePrincipal("owner", "dead")
    )
    outer = DishAdminApplication(
        conn,
        backend=Backend(task_gid="dish", title="Dish", section="pi"),
        invocation_request_id="22222222-2222-4222-8222-222222222222",
    )

    class FakeChild:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, command, **arguments):
            assert command == "kill"
            return {
                "ok": False,
                "command": "kill",
                "code": "CONFLICT",
                "data": {
                    "fenced_invocation": {"authority_state": "revoked"},
                    "outcome": "manual_reconciliation_required",
                    "human_consequence": "Revocation committed; reconciliation remains.",
                },
                "errors": [{"rule": "downstream_reconciliation_blocked"}],
            }

    monkeypatch.setattr(admin_module, "DishAdminApplication", FakeChild)

    result = admin_module._command_bulk_kill(
        outer,
        trace=admin_module.AdminTrace(),
        command="kill-all",
        expired_only=False,
        reason="replace unavailable runs",
        confirmed=True,
    )

    assert not result["ok"]
    assert result["data"]["selected_count"] == 1
    assert result["data"]["revoked_count"] == 1
    assert result["data"]["killed_count"] == 1
    assert result["data"]["failed_count"] == 0
    assert result["data"]["reconciliation_required_count"] == 1
    assert result["data"]["downstream_error_count"] == 1
    assert result["data"]["results"][0]["revoked"] is True
