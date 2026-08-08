from __future__ import annotations
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from dish_service import __main__ as service_main
from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import confirm_task_content, content_identity, create_operation
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import OperationActors, ResolvedRelease
from dish_tool.results import result_envelope
from dish_tool.task_store import write_exact_content

from tests.support.backend_service_resilience import (
    _release,
    ScopeRaceBackend,
    RejectedWriteBackend,
    ReturnedBaselineWithAdvancedVersionBackend,
)


def test_read_fails_if_task_leaves_cooking_between_scope_and_complete_reads(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    try:
        app = DishApplication(conn, ScopeRaceBackend(), release_loader=_release)
        result = app.execute("read", agent="gpt", task_gid="t")
    finally:
        conn.close()

    assert result["code"] == "UNMANAGED_TASK"
    assert result["errors"] == [{"rule": "task_not_in_cooking"}]

def test_start_does_not_open_operation_if_task_leaves_cooking_between_reads(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    try:
        app = DishApplication(conn, ScopeRaceBackend(), release_loader=_release)
        result = app.execute(
            "start", agent="gpt", task_gid="t", kind="planning", run_id="run"
        )
        operations = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    finally:
        conn.close()

    assert result["code"] == "UNMANAGED_TASK"
    assert operations == 0

def test_section_enumeration_follows_asana_pagination(monkeypatch):
    calls = []

    class SectionsApi:
        def __init__(self, _client):
            pass

        def get_sections_for_project(self, project_gid, options, **_kwargs):
            calls.append((project_gid, dict(options)))
            if "offset" not in options:
                return {
                    "data": [{"gid": str(index), "name": f"Section {index}"} for index in range(100)],
                    "next_page": {"offset": "page-2"},
                }
            return {
                "data": [{"gid": "100", "name": "Section 100"}],
                "next_page": None,
            }

    monkeypatch.setitem(sys.modules, "asana", SimpleNamespace(SectionsApi=SectionsApi))
    backend = AsanaBackend(api_client=object())
    sections = backend.list_sections(COOKING_PROJECT_GID)

    assert len(sections) == 101
    assert calls == [
        (COOKING_PROJECT_GID, {"opt_fields": "gid,name", "limit": 100}),
        (
            COOKING_PROJECT_GID,
            {"opt_fields": "gid,name", "limit": 100, "offset": "page-2"},
        ),
    ]

def test_unchanged_reread_preserves_nonretryable_backend_rejection(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    backend = RejectedWriteBackend()
    identity = confirm_task_content(
        conn, task_gid="t", title=backend.title, notes=backend.notes,
        schema_version="2", boundary="test",
    )
    operation = create_operation(
        conn,
        task_gid="t",
        operation_kind="planning",
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="rq",
        actors=OperationActors(editor_agent="gpt", run_id="run"),
    )
    try:
        with pytest.raises(BackendFailure) as exc:
            write_exact_content(
                conn,
                backend,
                operation_id=operation["operation_id"],
                task_gid="t",
                project_gid=COOKING_PROJECT_GID,
                expected_identity=identity.digest,
                expected_section_gid="rq",
                title="Changed",
                notes="Notes",
                schema_version="2",
            )
    finally:
        conn.close()

    assert exc.value.status == 403
    assert exc.value.rule == "backend_access_denied"
    assert exc.value.retryable is False

def test_release_loader_internal_typeerror_is_not_retried(tmp_path):
    calls = 0

    def loader(role=None, include_migrations=False):
        nonlocal calls
        calls += 1
        raise TypeError("loader implementation bug")

    service = DishService(
        ServiceConfig(db_path=tmp_path / "dish.db", honest_root=tmp_path),
        backend_factory=lambda: object(),
        release_loader=loader,
    )

    with pytest.raises(TypeError, match="implementation bug"):
        service._release("planning", include_migrations=True)
    assert calls == 1

def test_injected_backend_factory_resources_remain_caller_owned(tmp_path):
    class InjectedBackend:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    backend = InjectedBackend()
    service = DishService(
        ServiceConfig(db_path=tmp_path / "dish.db", honest_root=tmp_path),
        backend_factory=lambda: backend,
    )

    result = service.record_agent_argument_failure(
        "prepare",
        {"code": "INVALID_ARGUMENT", "message": "bad", "rule": "bad_argument"},
        {},
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert backend.closed is False

def test_owned_backend_cleanup_failure_does_not_replace_result_or_skip_db_close(
    monkeypatch, tmp_path
):
    class ClosingBackend:
        def close(self):
            raise RuntimeError("cleanup failed")

    class TrackingConnection:
        def __init__(self, conn):
            self.conn = conn
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def close(self):
            self.closed = True
            self.conn.close()

    raw = initialize_database(tmp_path / "dish.db")
    tracked = TrackingConnection(raw)
    service = DishService(ServiceConfig(db_path=tmp_path / "dish.db", honest_root=tmp_path))
    service.backend_factory = ClosingBackend
    monkeypatch.setattr(
        "dish_service.application.database_initialization.open_runtime_database",
        lambda _path: tracked,
    )

    result = service.record_agent_argument_failure(
        "prepare",
        {"code": "INVALID_ARGUMENT", "message": "bad", "rule": "bad_argument"},
        {},
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert tracked.closed is True

def test_admin_lease_cleanup_failure_preserves_committed_success(monkeypatch, tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = confirm_task_content(
        conn, task_gid="t", title="Title", notes="", schema_version="2", boundary="test"
    )
    operation = create_operation(
        conn,
        task_gid="t",
        operation_kind="planning",
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="rq",
    )
    principal = ServicePrincipal(owner_id="admin:marco", run_id="admin-run")
    leases = LeaseManager(conn, ttl_seconds=90)
    leases.acquire(operation["operation_id"], principal)
    service = DishService(
        ServiceConfig(db_path=tmp_path / "dish.db", honest_root=tmp_path),
        backend_factory=lambda: object(),
    )

    def fail_release(*_args, **_kwargs):
        raise RuntimeError("lease cleanup failed")

    monkeypatch.setattr(leases, "release", fail_release)
    result = service._release_admin_request_lease(
        result=result_envelope(
            command="recover", submission_id=operation["operation_id"], data={"committed": True}
        ),
        conn=conn,
        leases=leases,
        operation_id=operation["operation_id"],
        principal=principal,
        command="recover",
    )
    active = leases.active_for_operation(operation["operation_id"])
    conn.close()

    assert result["ok"] is True
    assert result["data"]["committed"] is True
    assert result["data"]["service_cleanup_warning"] == {
        "kind": "admin_lease_release",
        "operation_id": operation["operation_id"],
        "command": "recover",
        "error_type": "RuntimeError",
        "fallback_release_applied": True,
    }
    assert active is None

def test_second_listener_thread_start_failure_closes_both_without_shutdown_deadlock(
    monkeypatch,
):
    created_threads = []

    class FakeServer:
        def __init__(self):
            self.shutdown_calls = 0
            self.closed = False

        def shutdown(self):
            self.shutdown_calls += 1

        def server_close(self):
            self.closed = True

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.index = len(created_threads)
            self.joined = False
            created_threads.append(self)

        def start(self):
            if self.index == 1:
                raise RuntimeError("second thread failed")

        def join(self):
            self.joined = True

    monkeypatch.setattr(service_main.threading, "Thread", FakeThread)
    private = FakeServer()
    action = FakeServer()

    assert service_main._run_servers(private, action) == 1
    assert private.shutdown_calls == 1
    assert action.shutdown_calls == 0
    assert private.closed is True
    assert action.closed is True
    assert created_threads[0].joined is True
    assert created_threads[1].joined is False
