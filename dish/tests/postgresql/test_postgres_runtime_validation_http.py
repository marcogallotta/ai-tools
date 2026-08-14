from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
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
    assert service.supports_http_route("agent", "recover") is False
    assert service.supports_http_route("agent", "proposals") is True
    assert service.supports_http_route("agent", "apply-proposal") is True
    assert service.supports_http_route("agent", "safe-reclaim") is True
    assert service.supports_http_route("action", "sections") is True
    assert service.supports_http_route("action", "renew-lease") is True
    assert service.supports_http_route("action", "proposals") is True
    assert service.supports_http_route("action", "apply-proposal") is True
    assert service.supports_http_route("action", "safe-reclaim") is True
    assert service.supports_http_route("admin", "attention") is True
    assert service.supports_http_route("admin", "holds") is True
    assert service.supports_http_route("admin", "record-human-decision") is True
    assert service.supports_http_route("admin", "supply-evidence") is True
    assert service.supports_http_route("admin", "abandon-operation") is True
    assert service.supports_http_route("admin", "reconcile-abandonment") is True
    assert service.supports_http_route("admin-lease", "recover-lease") is True
    assert service.supports_http_route("admin-lease-expiry", "expire-lease") is True
    assert service.supports_http_route("admin", "backup-create") is False
    assert service.supports_http_route("admin-backup", "backup-create") is False

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



def test_production_runtime_accepts_preburn_deploy_and_enforces_postburn_fence(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, _task_id = workflow_db
    service = runtime_service(factory, tmp_path)
    service._expected_schema_head = "0002_core_authority_model"
    service._expected_release = "dish-42619b9"

    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        with pytest.raises(DishRuleError) as missing_epoch:
            service._production_boundary(session, generation)
        assert missing_epoch.value.rule == "postgresql_production_projection_epoch_missing"

        projection_epoch_id = _next(ids)
        activation_id = _next(ids)
        epoch = tx.ProjectionEpoch(
            projection_epoch_id=projection_epoch_id,
            generation_id=context["generation_id"],
            epoch_number=1,
            status="active",
            activation_reason="test production deployment authority",
            external_effects_enabled=True,
            created_at=NOW,
            retired_at=None,
        )
        session.add(epoch)
        session.flush()

        assert service._production_boundary(session, generation) == {
            "phase": "pre_rollback_burn",
            "projection_epoch_id": str(projection_epoch_id),
            "external_effects_enabled": True,
        }

        activation = models.AuthorityActivation(
            activation_id=activation_id,
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            cutover_approval_id="cutover-approval-1",
            legacy_bundle_id="legacy-bundle-1",
            registry_version_id=context["registry_version_id"],
            honest_binding_id=context["binding_id"],
            rehearsal_id=None,
            schema_head="0002_core_authority_model",
            dish_release="dish-42619b9",
            honest_release="honest-1",
            protocol_release="protocol-1",
            openapi_release="openapi-1",
            routing_release="routing-1",
            projection_epoch=projection_epoch_id,
            outcome="activated",
            rollback_burned_at=NOW,
            recorded_at=NOW,
        )
        session.add(activation)
        session.flush()

        with pytest.raises(DishRuleError) as projection:
            service._production_boundary(session, generation)
        assert projection.value.rule == "postgresql_production_projection_not_fenced"

        epoch.external_effects_enabled = False
        session.flush()
        boundary = service._production_boundary(session, generation)
        assert boundary == {
            "phase": "post_rollback_burn",
            "activation_id": str(activation_id),
            "rollback_burned_at": NOW.isoformat(),
            "projection_epoch_id": str(projection_epoch_id),
            "external_effects_enabled": False,
        }

        service._expected_release = "different-release"
        with pytest.raises(DishRuleError) as release:
            service._production_boundary(session, generation)
        assert release.value.rule == "postgresql_production_activation_identity_mismatch"


def test_postgresql_private_admin_transport_uses_admin_principal(
    workflow_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    missing_task_id = _next(ids)
    missing_operation_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner="marco-admin",
        )

    from dish_tool import backend as asana_backend

    def reject_asana_construction(*_args, **_kwargs):
        raise AssertionError("PostgreSQL admin path must not construct Asana")

    monkeypatch.setattr(asana_backend.AsanaBackend, "__init__", reject_asana_construction)
    service = runtime_service(factory, tmp_path)
    service._profile = "prod"
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-admin-http")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            attention_status, attention = _post_json(
                f"{base}/v1/admin/attention",
                token="postgres-admin-token",
                body={
                    "client": {
                        "run_id": str(run_id),
                        "request_id": str(request_id),
                    },
                    "arguments": {},
                },
            )
            human_status, human = _post_json(
                f"{base}/v1/admin/record-human-decision",
                token="postgres-admin-token",
                body={
                    "client": {
                        "run_id": str(run_id),
                        "request_id": str(_next(ids)),
                    },
                    "arguments": {
                        "task_id": str(missing_task_id),
                        "operation_id": str(missing_operation_id),
                        "detail": "A",
                        "rationale": "test routing only",
                    },
                },
            )
            agent_status, _agent = _post_json(
                f"{base}/v1/admin/attention",
                token="postgres-agent-token",
                body={
                    "client": {
                        "run_id": str(run_id),
                        "request_id": str(_next(ids)),
                    },
                    "arguments": {},
                },
            )
        finally:
            stop_server(server, thread)

    assert attention_status == 200
    assert attention["ok"] is True, attention
    assert attention["command"] == "attention"
    assert attention["data"]["read_only"] is True
    assert human_status == 200
    assert human["code"] in {"TASK_NOT_FOUND", "OPERATION_NOT_FOUND"}
    assert all(
        error.get("rule") != "principal_scope_rejected"
        for error in human.get("errors", [])
        if isinstance(error, dict)
    )
    assert agent_status == 403


def test_postgresql_admin_lease_bridges_keep_canonical_admin_transport(tmp_path: Path) -> None:
    service = PostgresRuntimeService.__new__(PostgresRuntimeService)
    service.config = ServiceConfig(
        db_path=tmp_path / "unused.sqlite3",
        honest_root=tmp_path,
        agent_token="postgres-agent-token",
        admin_token="postgres-admin-token",
        action_token="postgres-action-token",
        legacy_writer_fence_path=None,
    )
    lease_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    operation_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    principal = ServicePrincipal.from_values(
        "marco-admin", "11111111-1111-4111-8111-111111111111"
    )
    observed: list[tuple[str, dict[str, object], str]] = []

    service._active_actor_lease_target = lambda **_kwargs: (lease_id, operation_id)

    def execute_admin(command, arguments, *, principal, request_id):
        observed.append((command, dict(arguments), request_id))
        return {"ok": True, "command": command, "code": "OK", "data": {}}

    service.execute_admin = execute_admin

    service.recover_lease(
        str(operation_id),
        principal,
        reason="expired owner",
        request_id="55555555-5555-4555-8555-555555555555",
    )
    service.expire_lease(
        principal,
        lease_id=lease_id,
        reason="owner gone",
        request_id="66666666-6666-4666-8666-666666666666",
    )

    assert observed == [
        (
            "recover-lease",
            {
                "operation_id": str(operation_id),
                "lease_id": str(lease_id),
                "reason": "expired owner",
            },
            "55555555-5555-4555-8555-555555555555",
        ),
        (
            "expire-lease",
            {
                "operation_id": str(operation_id),
                "lease_id": str(lease_id),
                "reason": "owner gone",
            },
            "66666666-6666-4666-8666-666666666666",
        ),
    ]


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
