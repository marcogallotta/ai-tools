from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from sqlalchemy import func, select

from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.postgres_service import PostgresRuntimeService
from dish_pg.transition import ProjectionService
from dish_service.config import ServiceConfig
from dish_service.http import DishHTTPServer
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from tests.support.postgresql.runtime_validation import (
    runtime_service,
    without_replay_metadata,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.thread_teardown import start_server_thread, stop_server


def _post_json(
    url: str,
    *,
    body: dict[str, object],
    token: str = "postgres-agent-token",
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=3.0)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    with response:
        return response.status, json.loads(response.read().decode("utf-8"))


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

    service = runtime_service(factory, tmp_path)
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
    assert without_replay_metadata(replay) == first
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

    result = runtime_service(factory, tmp_path).execute_agent(
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


def test_http_postgresql_execution_unavailable_is_503_not_validation_persistence(
    tmp_path: Path,
) -> None:
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
    validation_calls = 0

    def fail_execute(*args, **kwargs):
        raise DishRuleError(
            "BACKEND_REJECTED",
            "PostgreSQL authority is unavailable; governed mutation was not admitted",
            rule="postgresql_authority_unavailable",
            retryable=True,
            details={"error_type": "OperationalError"},
        )

    def reject_validation_persistence(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        raise AssertionError("execution failures must not enter validation persistence")

    service.execute_agent = fail_execute
    service.record_replay_validation_failure = reject_validation_persistence
    body = {
        "client": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "request_id": "22222222-2222-4222-8222-222222222222",
        },
        "arguments": {"title": "Must not commit while PostgreSQL is down"},
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-execution-unavailable-http")
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/commands/create"
        try:
            status, result = _post_json(url, body=body)
        finally:
            stop_server(server, thread)

    assert status == 503
    assert result["ok"] is False
    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is True
    assert result["errors"] == [
        {"error_type": "OperationalError", "rule": "postgresql_authority_unavailable"}
    ]
    assert result["data"]["message"] == (
        "PostgreSQL authority is unavailable; governed mutation was not admitted"
    )
    assert validation_calls == 0


def test_http_postgresql_validation_persistence_unavailable_is_503(
    tmp_path: Path,
) -> None:
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
    validation_calls = 0

    def reject_execute(*args, **kwargs):
        raise AssertionError("HTTP validation failure must happen before dispatch")

    def fail_validation_persistence(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        raise DishRuleError(
            "BACKEND_REJECTED",
            "PostgreSQL authority is unavailable; validation failure was not recorded",
            rule="postgresql_authority_unavailable",
            retryable=True,
            details={"error_type": "OperationalError"},
        )

    service.execute_agent = reject_execute
    service.record_replay_validation_failure = fail_validation_persistence
    body = {
        "client": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "request_id": "22222222-2222-4222-8222-222222222222",
        },
        "arguments": {"operation_id": "not-a-uuid"},
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-validation-persistence-unavailable-http")
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/commands/create"
        try:
            status, result = _post_json(url, body=body)
        finally:
            stop_server(server, thread)

    assert status == 503
    assert result["ok"] is False
    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is True
    assert result["errors"] == [
        {"error_type": "OperationalError", "rule": "postgresql_authority_unavailable"}
    ]
    assert result["data"]["message"] == (
        "PostgreSQL authority is unavailable; validation failure was not recorded"
    )
    assert validation_calls == 1


def test_postgresql_runtime_exposes_only_implemented_action_commands(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
        )

    service = runtime_service(factory, tmp_path)
    service.config = replace(
        service.config,
        action_token="postgres-action-token",
        action_public_base_url="https://dish-pg-test.example.invalid/test",
    )
    assert service.supports_http_route("agent", "recover") is True
    assert service.supports_http_route("agent", "proposals") is True
    assert service.supports_http_route("agent", "apply-proposal") is True
    assert service.supports_http_route("agent", "safe-reclaim") is True
    assert service.supports_http_route("action", "sections") is True
    assert service.supports_http_route("action", "renew-lease") is True
    assert service.supports_http_route("action", "proposals") is True
    assert service.supports_http_route("action", "apply-proposal") is True
    assert service.supports_http_route("action", "safe-reclaim") is True

    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="action") as server:
        thread = start_server_thread(server, name="postgres-action-http")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(
            f"{base}/v1/action/sections",
            data=json.dumps(
                {"client": {"run_id": str(run_id)}, "arguments": {"agent": "gpt"}}
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer postgres-action-token",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3.0) as response:
                result = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"{base}/openapi/action.json",
                    headers={"Authorization": "Bearer postgres-action-token"},
                ),
                timeout=3.0,
            ) as response:
                openapi = json.loads(response.read().decode("utf-8"))
        finally:
            stop_server(server, thread)

    assert result["ok"] is True
    assert result["command"] == "sections"
    assert openapi["servers"] == [{"url": "https://dish-pg-test.example.invalid/test"}]
    assert "/v1/action/sections" in openapi["paths"]
    assert "/v1/action/renew-lease" in openapi["paths"]
    assert "/v1/action/proposals" in openapi["paths"]
    assert "/v1/action/apply-proposal" in openapi["paths"]
    assert "/v1/action/safe-reclaim" in openapi["paths"]


def test_postgresql_action_canonical_ids_work_without_asana_identity(
    workflow_db, tmp_path: Path, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner="gpt-action",
            agent="gpt",
        )
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="canonical Action identity test authority",
            created_at=NOW,
            external_effects_enabled=True,
        )

    from dish_tool import backend as asana_backend

    def reject_asana_construction(*_args, **_kwargs):
        raise AssertionError("PostgreSQL canonical Action path must not construct Asana")

    monkeypatch.setattr(asana_backend.AsanaBackend, "__init__", reject_asana_construction)
    service = runtime_service(factory, tmp_path)
    service.config = replace(
        service.config,
        action_token="postgres-action-token",
        action_public_base_url="https://dish-pg-test.example.invalid/test",
    )
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="action") as server:
        thread = start_server_thread(server, name="postgres-canonical-action-http")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            read_status, read_result = _post_json(
                f"{base}/v1/action/read", token="postgres-action-token",
                body={"client": {"run_id": str(run_id)}, "arguments": {"dish_id": str(task_id), "agent": "gpt"}},
            )
            section_status, section_result = _post_json(
                f"{base}/v1/action/section-tasks", token="postgres-action-token",
                body={"client": {"run_id": str(run_id)}, "arguments": {"section_id": str(context["section_id"]), "agent": "gpt"}},
            )
            alias_read_status, alias_read_result = _post_json(
                f"{base}/v1/action/read", token="postgres-action-token",
                body={"client": {"run_id": str(run_id)}, "arguments": {"task_gid": "123456789", "agent": "gpt"}},
            )
            alias_section_status, alias_section_result = _post_json(
                f"{base}/v1/action/section-tasks", token="postgres-action-token",
                body={"client": {"run_id": str(run_id)}, "arguments": {"section_gid": "1217084805070731", "agent": "gpt"}},
            )
            proposals_status, proposals_result = _post_json(
                f"{base}/v1/action/proposals",
                token="postgres-action-token",
                body={
                    "client": {"run_id": str(run_id)},
                    "arguments": {"agent": "gpt"},
                },
            )
            start_status, start_result = _post_json(
                f"{base}/v1/action/start", token="postgres-action-token",
                body={"client": {"run_id": str(run_id), "request_id": str(_next(ids))}, "arguments": {"dish_id": str(task_id), "agent": "gpt", "kind": "initial"}},
            )
            create_status, create_result = _post_json(
                f"{base}/v1/action/create", token="postgres-action-token",
                body={"client": {"run_id": str(run_id), "request_id": str(_next(ids))}, "arguments": {"agent": "gpt", "title": "Canonical create"}},
            )
        finally:
            stop_server(server, thread)

    assert read_status == 200
    assert read_result["data"]["dish_id"] == str(task_id)
    assert section_status == 200
    assert section_result["data"]["tasks"][0]["dish_id"] == str(task_id)
    assert alias_read_status == 200
    assert alias_read_result["data"]["dish_id"] == str(task_id)
    assert alias_section_status == 200
    assert alias_section_result["data"]["tasks"][0]["dish_id"] == str(task_id)
    assert proposals_status == 200
    assert proposals_result["data"]["count"] == 0
    assert proposals_result["data"]["proposals"] == []
    assert proposals_result["data"]["instruction"] == (
        "Claim and apply an approved PostgreSQL proposal exactly as stored; "
        "do not reconstruct or edit its candidate."
    )
    assert start_status == 200 and start_result["ok"] is True
    assert create_status == 200 and create_result["ok"] is True
    assert create_result["data"]["dish_id"] == create_result["data"]["task_id"]


def test_postgresql_runtime_renew_lease_reuses_command_port(
    workflow_db, tmp_path: Path, monkeypatch
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
        )

    service = runtime_service(factory, tmp_path)
    captured: dict[str, object] = {}

    def execute(command, arguments, *, principal, request_id):
        captured.update(
            command=command,
            arguments=dict(arguments),
            principal=principal,
            request_id=request_id,
        )
        return {"ok": True}

    monkeypatch.setattr(service, "execute_agent", execute)
    principal = ServicePrincipal.from_values("gpt-action", str(run_id))
    result = service.renew_lease(
        "00000000-0000-4000-8000-000000000111",
        principal,
        request_id=str(request_id),
    )

    assert result == {"ok": True}
    assert captured == {
        "command": "renew-lease",
        "arguments": {"operation_id": "00000000-0000-4000-8000-000000000111"},
        "principal": principal,
        "request_id": str(request_id),
    }
