from __future__ import annotations

import threading
import uuid

import pytest

from dish_service import admin_cli
from dish_service.application import DishService
from dish_service.client import DishAdminServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.database import (
    complete_operation_step,
    confirm_task_content,
    create_abandonment_attempt_in_transaction,
    create_operation,
    declare_operation_step,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from tests.support.thread_teardown import join_thread, start_server_thread, stop_server
from tests.support.abandonment import Backend, _NUMERIC_TASK_GID, _numeric_task_source, _source
from tests.support.abandonment_admin import _released_actor_lease
from tests.support.service_foundation import _release_loader

_TASK_URL = f"https://app.asana.com/0/999888777666555/{_NUMERIC_TASK_GID}"


class _RecordingAdminServiceClient(DishAdminServiceClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, command, arguments=None, **keyword_arguments):
        prepared = dict(arguments or keyword_arguments)
        self.calls.append((command, prepared))
        return {
            "ok": True,
            "command": command,
            "code": "OK",
            "task_gid": prepared.get("task_gid"),
            "submission_id": None,
            "state": None,
            "retryable": False,
            "allowed_actions": [],
            "data": {},
            "errors": [],
        }


@pytest.mark.parametrize(
    ("reference", "expected_arguments"),
    [
        (
            "12345678-1234-5678-9234-567812345678",
            {
                "dish_id": "12345678-1234-5678-9234-567812345678",
                "reason": "cook again",
            },
        ),
        (
            _NUMERIC_TASK_GID,
            {"task_gid": _NUMERIC_TASK_GID, "reason": "cook again"},
        ),
    ],
)
def test_admin_cli_reopen_planning_routes_canonical_and_legacy_identity(
    reference, expected_arguments, capsys
):
    client = _RecordingAdminServiceClient()

    status = admin_cli.main(
        ["reopen-planning", reference, "--reason", "cook again"],
        application=client,
    )

    assert status == 0
    assert client.calls == [("reopen-planning", expected_arguments)]
    assert capsys.readouterr().err == ""





def test_abandon_operation_resolves_task_gid_to_open_operation():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)
    lease = _released_actor_lease(conn, source["operation_id"])
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute(
        "abandon-operation",
        submission_id=_NUMERIC_TASK_GID,
        lease_id=lease["lease_id"],
        reason="the original conversation is permanently unavailable",
    )

    assert result["ok"]
    assert result["submission_id"] == source["operation_id"]


def test_abandon_operation_resolves_asana_task_url_to_open_operation():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)
    lease = _released_actor_lease(conn, source["operation_id"])
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute(
        "abandon-operation",
        submission_id=_TASK_URL,
        lease_id=lease["lease_id"],
        reason="the original conversation is permanently unavailable",
    )

    assert result["ok"]
    assert result["submission_id"] == source["operation_id"]


def test_abandon_operation_task_gid_with_no_open_operation_fails_not_found():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute(
        "abandon-operation",
        submission_id=_NUMERIC_TASK_GID,
        reason="the original conversation is permanently unavailable",
    )

    assert not result["ok"]
    assert result["code"] == "NOT_FOUND"
    assert any(
        item.get("rule") == "admin_operation_target_not_found"
        for item in result["errors"]
    )


def test_reconcile_abandonment_resolves_task_gid_to_active_abandonment():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)
    lease = _released_actor_lease(conn, source["operation_id"])
    conn.execute("BEGIN IMMEDIATE")
    create_abandonment_attempt_in_transaction(
        conn, abandonment_id="abandonment-1", task_gid=_NUMERIC_TASK_GID,
        source_operation_id=source["operation_id"], source_lease_id=lease["lease_id"],
        abandoned_owner_id="owner", abandoned_run_id="dead-run",
        reason="conversation permanently unavailable",
    )
    conn.execute("COMMIT")

    row = conn.execute(
        "SELECT abandonment_id FROM abandonment_attempts WHERE task_gid=?",
        (_NUMERIC_TASK_GID,),
    ).fetchone()
    assert row is not None

    from dish_tool.database import resolve_admin_abandonment_target

    assert resolve_admin_abandonment_target(conn, _NUMERIC_TASK_GID) == "abandonment-1"
    assert resolve_admin_abandonment_target(conn, _TASK_URL) == "abandonment-1"


def test_service_execute_admin_resolves_task_gid_for_abandon_operation(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)
    lease = _released_actor_lease(conn, source["operation_id"])
    conn.close()

    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=honest),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    principal = ServicePrincipal(str(uuid.uuid4()), str(uuid.uuid4()))

    result = service.execute_admin(
        "abandon-operation",
        {
            "submission_id": _NUMERIC_TASK_GID,
            "lease_id": lease["lease_id"],
            "reason": "the original conversation is permanently unavailable",
        },
        principal=principal,
        request_id=str(uuid.uuid4()),
    )

    assert result["ok"], result
    assert result["submission_id"] == source["operation_id"]


def test_service_execute_admin_task_gid_with_no_open_operation_fails_not_found(tmp_path):
    db_path = tmp_path / "dish.db"
    initialize_database(db_path).close()
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)

    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=honest),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    principal = ServicePrincipal(str(uuid.uuid4()), str(uuid.uuid4()))

    result = service.execute_admin(
        "abandon-operation",
        {
            "submission_id": _NUMERIC_TASK_GID,
            "reason": "the original conversation is permanently unavailable",
        },
        principal=principal,
        request_id=str(uuid.uuid4()),
    )

    assert not result["ok"]
    assert result["code"] == "NOT_FOUND"
    assert any(
        item.get("rule") == "admin_operation_target_not_found"
        for item in result["errors"]
    )


def test_real_http_admin_client_abandons_by_task_gid_with_no_lease_id(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)
    _released_actor_lease(conn, source["operation_id"])
    conn.close()

    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=db_path,
            honest_root=honest,
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    client = DishAdminServiceClient(
        f"http://{host}:{port}", token="admin-secret", run_id=str(uuid.uuid4())
    )
    try:
        result = client.execute(
            "abandon-operation",
            submission_id=_NUMERIC_TASK_GID,
            lease_id=None,
            reason="the original conversation is permanently unavailable",
        )
    finally:
        stop_server(server, thread)
        assert not thread.is_alive()

    assert result["ok"], result
    assert result["submission_id"] == source["operation_id"]


def test_resolve_admin_operation_target_passes_through_exact_operation_id():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)

    from dish_tool.database import resolve_admin_operation_target

    assert resolve_admin_operation_target(conn, source["operation_id"]) == source["operation_id"]


def test_admin_inspect_resolves_task_gid_and_explains_current_operation():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi", task_gid=_NUMERIC_TASK_GID)
    source = _numeric_task_source(conn, backend)
    app = DishAdminApplication(conn, backend=backend)

    result = app.execute("inspect", submission_id=_NUMERIC_TASK_GID)

    assert result["ok"]
    assert result["submission_id"] == source["operation_id"]
    assert result["task_gid"] == _NUMERIC_TASK_GID
    assert result["data"]["task_title"] == backend.title
    assert result["data"]["problem"]
    assert isinstance(result["data"]["human_actions"], list)


def test_admin_inspect_prioritizes_hold_resolution_over_historical_lease():
    conn = initialize_database(":memory:")
    backend = Backend(section="rq")
    source = _source(conn, backend, kind="initial", phase="held_human")
    declare_operation_step(
        conn,
        source["operation_id"],
        "research_preconstruction_hold",
        {
            "route": "human-review",
            "resume_status": "pending-research",
            "candidate_content_existed": False,
        },
    )
    complete_operation_step(
        conn, source["operation_id"], "research_preconstruction_hold"
    )
    _released_actor_lease(conn, source["operation_id"])

    result = DishAdminApplication(conn, backend=backend).execute(
        "inspect", submission_id=source["operation_id"]
    )

    assert result["ok"], result
    assert result["data"]["agent_actions_now"] == []
    actions = result["data"]["human_actions"]
    assert len(actions) == 1
    assert actions[0]["command"] == "review-inspect"
    assert "abandon-operation" not in actions[0]["shell_command"]
