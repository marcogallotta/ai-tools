from __future__ import annotations

import copy
import json
import urllib.request
from pathlib import Path

from sqlalchemy import func, select

from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.postgres_service import PostgresRuntimeService
from dish_service.config import ServiceConfig
from dish_service.http import DishHTTPServer
from dish_service.leases import ServicePrincipal
from tests.support.postgresql.workflow import _next, _register_run, workflow_db
from tests.support.thread_teardown import start_server_thread, stop_server

SECRET = b"postgres-validation-replay-secret"


def _runtime_service(factory, tmp_path: Path) -> PostgresRuntimeService:
    service = PostgresRuntimeService.__new__(PostgresRuntimeService)
    service.config = ServiceConfig(
        db_path=tmp_path / "unused.sqlite3",
        honest_root=tmp_path,
        port=0,
        action_port=0,
        agent_token="postgres-agent-token",
        admin_token="postgres-admin-token",
        action_token=None,
        legacy_writer_fence_path=None,
    )
    service._session_maker = factory
    service._cursor_secret = SECRET
    return service


def _post_json(url: str, *, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer postgres-agent-token",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=3.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _without_replay_metadata(payload: dict[str, object]) -> dict[str, object]:
    normalized = copy.deepcopy(payload)
    data = normalized.get("data")
    assert isinstance(data, dict)
    data.pop("request_replayed", None)
    return normalized


def test_http_first_and_replay_envelopes_differ_only_by_replay_metadata(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner="cli",
        )

    service = _runtime_service(factory, tmp_path)
    body = {
        "client": {"run_id": str(run_id), "request_id": str(request_id)},
        "arguments": {"operation_id": "not-a-uuid"},
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-validation-http")
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/commands/create"
        try:
            first_status, first = _post_json(url, body=body)
            replay_status, replay = _post_json(url, body=body)
        finally:
            stop_server(server, thread)

    assert first_status == replay_status == 200
    assert first["errors"] == replay["errors"]
    assert first["errors"][0]["rule"] == "uuid_identifier_required"
    assert first["data"]["message"] == (
        "operation_id must be a non-nil canonical lowercase UUID in 8-4-4-4-12 form"
    )
    assert first["data"]["request_id"] == str(request_id)
    assert first["retryable"] is False
    assert "request_replayed" not in first["data"]
    assert replay["data"]["request_replayed"] is True
    assert _without_replay_metadata(replay) == first
    with session_scope(factory) as session:
        assert int(
            session.scalar(
                select(func.count())
                .select_from(wf.ServiceRequest)
                .where(wf.ServiceRequest.request_id == request_id)
            )
            or 0
        ) == 1
        assert int(
            session.scalar(
                select(func.count())
                .select_from(wf.ServiceRequestOutcome)
                .where(wf.ServiceRequestOutcome.request_id == request_id)
            )
            or 0
        ) == 1
        assert int(
            session.scalar(
                select(func.count())
                .select_from(wf.CommandExecution)
                .where(wf.CommandExecution.request_id == request_id)
            )
            or 0
        ) == 0


def test_normal_postgresql_runtime_query_is_unchanged(workflow_db, tmp_path: Path) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
        )

    result = _runtime_service(factory, tmp_path).execute_agent(
        "sections",
        {},
        principal=ServicePrincipal.from_values("owner-1", str(run_id)),
        request_id=None,
    )

    assert result["ok"] is True
    assert result["code"] == "OK"
    assert result["data"]["request_replayed"] is False
    assert result["data"]["sections"]
    with session_scope(factory) as session:
        assert int(session.scalar(select(func.count()).select_from(wf.ServiceRequest)) or 0) == 0
        assert int(
            session.scalar(select(func.count()).select_from(wf.ServiceRequestOutcome)) or 0
        ) == 0
