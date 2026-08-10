from __future__ import annotations

import json

from dish_service import admin_cli
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.admin_human import render_admin_result
from dish_tool.database import confirm_task_content
from dish_tool.database_initialization import initialize_database
from dish_tool.identifiers import stable_dish_uuid_for_asana_identity
from tests.support.abandonment import Backend
from tests.test_admin_task_target_resolution import _NUMERIC_TASK_GID, _numeric_task_source


def _dish_id() -> str:
    return str(stable_dish_uuid_for_asana_identity("task", _NUMERIC_TASK_GID))


def test_verbose_inspect_exposes_durable_operation_authority_and_history():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    LeaseManager(conn).acquire(
        operation["operation_id"], ServicePrincipal("owner", "run-verbose")
    )
    app = DishAdminApplication(conn, backend=backend)

    compact = app.execute("inspect", dish=_dish_id())
    verbose = app.execute("inspect", dish=_dish_id(), verbose=True)

    assert compact["ok"] is True
    assert compact["data"]["diagnostics"] is None
    diagnostics = verbose["data"]["diagnostics"]
    assert diagnostics["operation"]["operation_id"] == operation["operation_id"]
    assert diagnostics["operation_history"][0]["operation_id"] == operation["operation_id"]
    assert diagnostics["service_leases"][0]["run_id"] == "run-verbose"
    assert any(event["event_type"] == "operation.created" for event in diagnostics["recent_audit_events"])

    rendered = render_admin_result(verbose, profile="test", verbose=True)
    assert "Technical diagnostics" in rendered
    assert "Operation detail:" in rendered
    assert "Authority" in rendered
    assert "run=run-verbose" in rendered
    assert "Recent durable events" in rendered


def test_verbose_inspect_resting_dish_shows_content_and_historical_evidence():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID, title="Resting Dish")
    confirm_task_content(
        conn,
        task_gid=_NUMERIC_TASK_GID,
        title=backend.title,
        notes=backend.notes,
        schema_version="2",
        boundary="test-baseline",
    )
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute("inspect", dish=_dish_id(), verbose=True)

    assert result["ok"] is True
    assert result["state"] == "resting"
    diagnostics = result["data"]["diagnostics"]
    assert diagnostics["content_head"]["last_confirmed_title"] == "Resting Dish"
    assert diagnostics["operation_history"] == []


def test_admin_cli_verbose_inspect_requests_backend_diagnostics(capsys):
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    operation = _numeric_task_source(conn, backend)
    app = DishAdminApplication(conn, backend=backend)

    assert admin_cli.main(["--profile", "test", "--verbose", "inspect", _dish_id()], application=app) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["data"]["diagnostics"]["operation"]["operation_id"] == operation["operation_id"]
