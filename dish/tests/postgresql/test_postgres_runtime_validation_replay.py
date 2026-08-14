from __future__ import annotations

import copy
import inspect
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.postgres_service import PostgresRuntimeService
from dish_pg.recovery_control import _clone_registry
from dish_pg.workflow import (
    MutationAdmissionClosed,
    RequestSpec,
    VALIDATION_FAILURE_REQUEST_KIND,
    WorkflowAuthorityService,
    sha256_json,
)
from dish_service.http import DishHTTPServer
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from tests.support.postgresql.concurrency import run_concurrent_workers, wait_at_barrier
from tests.support.postgresql.release import HASH_A, _prepare_candidate
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


def test_runtime_validation_adapter_delegates_replay_lifecycle_to_command_port() -> None:
    source = inspect.getsource(PostgresRuntimeService.record_replay_validation_failure)
    assert ".record_validation_failure(" in source
    for duplicated_owner in (
        "WorkflowAuthorityService(",
        "RequestSpec(",
        "StoredOutcome(",
        "RegistryRepository(",
    ):
        assert duplicated_owner not in source


def _validation_error(field: str = "operation_id") -> DishRuleError:
    return DishRuleError(
        "INVALID_ARGUMENT",
        f"{field} must be a canonical UUID",
        rule="uuid_identifier_required",
        details={
            "field": field,
            "expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        },
    )


def _row_counts(session, request_id: uuid.UUID) -> dict[str, int]:
    models_by_name = {
        "requests": wf.ServiceRequest,
        "outcomes": wf.ServiceRequestOutcome,
        "executions": wf.CommandExecution,
        "audits": wf.GovernedAuditEvent,
        "obligations": wf.InvocationAuditObligation,
    }
    return {
        name: int(
            session.scalar(
                select(func.count()).select_from(model).where(
                    model.request_id == request_id
                )
            )
            or 0
        )
        for name, model in models_by_name.items()
    }


def _expected_counts() -> dict[str, int]:
    return {
        "requests": 1,
        "outcomes": 1,
        "executions": 0,
        "audits": 1,
        "obligations": 1,
    }


def _validation_runtime(workflow_db, tmp_path: Path):
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
    return (
        factory,
        ids,
        context,
        task_id,
        runtime_service(factory, tmp_path),
        ServicePrincipal.from_values("owner-1", str(run_id)),
        run_id,
        request_id,
        {"operation_id": "not-a-uuid"},
    )


def _seed_closed_reservation(factory, ids, context, task_id):
    with session_scope(factory) as session:
        _service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        reservation_run_id = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=reservation_run_id,
            agent="service",
        )
        cutover_id = _next(ids)
        plan_id = _next(ids)
        reserved_request_id = _next(ids)
        reserved_payload = {
            "command": "start",
            "arguments": {"task_id": str(task_id)},
        }
        session.add(
            rel.CutoverRun(
                cutover_run_id=cutover_id,
                candidate_id=candidate_id,
                state="admission_open",
                state_revision=5,
                started_at=NOW,
                terminal_at=None,
            )
        )
        session.add(
            rel.FirstAdmissionPlan(
                plan_id=plan_id,
                cutover_run_id=cutover_id,
                request_id=reserved_request_id,
                command_name="start",
                task_id=task_id,
                expected_projection_events=1,
                payload=reserved_payload,
                plan_sha256=HASH_A,
                recorded_at=NOW,
            )
        )
        session.flush()
        reservation_id = _next(ids)
        session.add(
            reservations.FirstRequestReservation(
                reservation_id=reservation_id,
                plan_id=plan_id,
                cutover_run_id=cutover_id,
                candidate_id=candidate_id,
                generation_id=context["generation_id"],
                request_id=reserved_request_id,
                command_name="start",
                owner_id="owner-1",
                principal_class="service",
                run_id=reservation_run_id,
                canonical_payload_sha256=sha256_json(reserved_payload),
                state="reserved",
                reservation_revision=1,
                reserved_at=NOW,
                consumed_at=None,
            )
        )
        session.flush()
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None and control.state == "closed"
    return reservation_id


def _reservation_state(session, reservation_id: uuid.UUID) -> dict[str, object]:
    reservation = session.get(reservations.FirstRequestReservation, reservation_id)
    assert reservation is not None
    return {
        "request_id": reservation.request_id,
        "state": reservation.state,
        "reservation_revision": reservation.reservation_revision,
        "consumed_at": reservation.consumed_at,
    }


def test_validation_failure_persists_and_exactly_replays(workflow_db, tmp_path: Path) -> None:
    factory, _ids, _context, _task_id, service, principal, run_id, request_id, arguments = (
        _validation_runtime(workflow_db, tmp_path)
    )
    first = service.record_replay_validation_failure(
        "create",
        arguments,
        principal=principal,
        request_id=str(request_id),
        error=_validation_error(),
    )

    assert first["ok"] is False
    assert first["code"] == "INVALID_ARGUMENT"
    assert first["retryable"] is False
    assert first["errors"] == [
        {
            "rule": "uuid_identifier_required",
            "field": "operation_id",
            "expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        }
    ]
    assert first["data"] == {
        "message": "operation_id must be a canonical UUID",
        "request_id": str(request_id),
    }

    with session_scope(factory) as session:
        request = session.get(wf.ServiceRequest, request_id)
        assert request is not None
        assert request.canonical_payload == {
            "request_kind": VALIDATION_FAILURE_REQUEST_KIND,
            "command": "create",
            "arguments": arguments,
            "owner_id": "owner-1",
            "run_id": str(run_id),
            "validation_error": {
                "code": "INVALID_ARGUMENT",
                "retryable": False,
                "message": "operation_id must be a canonical UUID",
                "errors": first["errors"],
            },
        }
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        assert outcome is not None
        assert dict(outcome.result_payload) == first
        assert _row_counts(session, request_id) == _expected_counts()

    replay = service.record_replay_validation_failure(
        "create",
        arguments,
        principal=principal,
        request_id=str(request_id),
        error=_validation_error(),
    )
    assert replay["data"]["request_replayed"] is True
    assert without_replay_metadata(replay) == first
    with session_scope(factory) as session:
        assert _row_counts(session, request_id) == _expected_counts()


def test_http_agent_validation_failure_preserves_agent_request_identity(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session, generation_id=context["generation_id"], run_id=run_id, owner="cli"
        )

    service = runtime_service(factory, tmp_path)
    body = {
        "client": {"run_id": str(run_id), "request_id": str(request_id)},
        "arguments": {"operation_id": "not-a-uuid"},
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-agent-validation-replay")
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/commands/create"
        try:
            status, result = _post_json(url, body=body)
        finally:
            stop_server(server, thread)

    assert status == 200
    assert result["errors"][0]["rule"] == "uuid_identifier_required"
    with session_scope(factory) as session:
        request = session.get(wf.ServiceRequest, request_id)
        assert request is not None
        assert request.owner_id == "cli"
        assert request.principal_class == "agent"
        assert request.command_name == "create"


def test_http_admin_validation_failure_preserves_admin_request_identity_and_replay(
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
            owner="marco-admin",
        )

    service = runtime_service(factory, tmp_path)
    service._profile = "prod"
    first_body = {
        "client": {"run_id": str(run_id), "request_id": str(request_id)},
        "arguments": {"operation_id": "not-a-uuid"},
    }
    conflicting_body = {
        "client": {"run_id": str(run_id), "request_id": str(request_id)},
        "arguments": {"operation_id": "also-not-a-uuid"},
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-admin-validation-replay")
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/admin/recover"
        try:
            first_status, first = _post_json(
                url, body=first_body, token="postgres-admin-token"
            )
            replay_status, replay = _post_json(
                url, body=first_body, token="postgres-admin-token"
            )
            conflict_status, conflict = _post_json(
                url, body=conflicting_body, token="postgres-admin-token"
            )
        finally:
            stop_server(server, thread)

    assert first_status == replay_status == conflict_status == 200
    assert first["errors"][0]["rule"] == "uuid_identifier_required"
    assert "request_replayed" not in first["data"]
    assert replay["data"]["request_replayed"] is True
    assert without_replay_metadata(replay) == first
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"
    with session_scope(factory) as session:
        request = session.get(wf.ServiceRequest, request_id)
        assert request is not None
        assert request.owner_id == "marco-admin"
        assert request.principal_class == "admin"
        assert request.command_name == "recover"
        assert request.canonical_payload["arguments"] == {"operation_id": "not-a-uuid"}
        assert _row_counts(session, request_id) == _expected_counts()


def test_http_admin_lease_validation_failure_preserves_admin_request_identity(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    operation_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner="marco-admin",
        )

    service = runtime_service(factory, tmp_path)
    service._profile = "prod"
    body = {
        "client": {"run_id": str(run_id), "request_id": str(request_id)},
        "reason": "",
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-admin-lease-validation-replay")
        url = (
            f"http://127.0.0.1:{server.server_address[1]}"
            f"/v1/admin/leases/{operation_id}/recover"
        )
        try:
            status, result = _post_json(url, body=body, token="postgres-admin-token")
        finally:
            stop_server(server, thread)

    assert status == 200
    assert result["errors"][0]["rule"] == "recovery_reason_required"
    with session_scope(factory) as session:
        request = session.get(wf.ServiceRequest, request_id)
        assert request is not None
        assert request.owner_id == "marco-admin"
        assert request.principal_class == "admin"
        assert request.command_name == "recover-lease"
        assert request.canonical_payload["arguments"] == {
            "operation_id": str(operation_id),
            "reason": "",
        }
        assert _row_counts(session, request_id) == _expected_counts()


def test_closed_mutation_admission_does_not_consume_first_request_reservation(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, task_id, service, principal, _run_id, request_id, arguments = (
        _validation_runtime(workflow_db, tmp_path)
    )
    reservation_id = _seed_closed_reservation(factory, ids, context, task_id)
    with session_scope(factory) as session:
        before = _reservation_state(session, reservation_id)
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None and control.state == "closed"

    result = service.record_replay_validation_failure(
        "create",
        arguments,
        principal=principal,
        request_id=str(request_id),
        error=_validation_error(),
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["data"]["message"] == "operation_id must be a canonical UUID"
    with session_scope(factory) as session:
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None and control.state == "closed"
        assert _reservation_state(session, reservation_id) == before
        assert _row_counts(session, request_id) == _expected_counts()


def test_destructive_restore_gate_does_not_replace_validation_error(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        predecessor = session.get(models.AuthorityGeneration, context["generation_id"])
        assert predecessor is not None
        predecessor.status = "retired"
        predecessor.retired_at = NOW
        session.flush()
        generation_id = _next(ids)
        bootstrap_id = _next(ids)
        capability = run_id.bytes + run_id.bytes
        session.add(
            models.AuthorityGeneration(
                generation_id=generation_id,
                predecessor_generation_id=predecessor.generation_id,
                creation_reason="destructive_restore",
                external_restore_control_id="restore-control-validation-test",
                schema_head=predecessor.schema_head,
                dish_release=predecessor.dish_release,
                status="active",
                created_at=NOW,
                retired_at=None,
            )
        )
        session.add(
            models.GenerationBootstrapAuthority(
                bootstrap_id=bootstrap_id,
                generation_id=generation_id,
                external_control_id="bootstrap-validation-test",
                capability_digest=capability,
                issued_at=NOW,
                consumed_at=None,
                retired_at=None,
            )
        )
        session.flush()
        # A destructive-restore generation owns an exact governed registry.  Do not
        # let this fixture depend on the retired same-release binding fallback that
        # runtime identity resolution deliberately no longer permits.
        _clone_registry(
            session,
            predecessor_generation_id=predecessor.generation_id,
            generation_id=generation_id,
            at=NOW,
            uuid_factory=lambda: _next(ids),
        )
        WorkflowAuthorityService(session).register_run(
            run_id=run_id,
            generation_id=generation_id,
            owner_id="owner-1",
            agent="claude",
            capability_digest=capability,
            bootstrap_id=bootstrap_id,
            registered_at=NOW,
        )

    with pytest.raises(MutationAdmissionClosed):
        with session_scope(factory) as session:
            WorkflowAuthorityService(session).admit_request(
                RequestSpec(
                    request_id=_next(ids),
                    generation_id=generation_id,
                    run_id=run_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    command_name="create",
                    canonical_payload={"command": "create", "arguments": {}},
                    protocol_release="protocol-1",
                    dish_release=predecessor.dish_release,
                    admitted_at=NOW,
                )
            )

    result = runtime_service(factory, tmp_path).record_replay_validation_failure(
        "create",
        {"operation_id": "not-a-uuid"},
        principal=ServicePrincipal.from_values("owner-1", str(run_id)),
        request_id=str(request_id),
        error=_validation_error(),
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "uuid_identifier_required"
    with session_scope(factory) as session:
        assert _row_counts(session, request_id) == _expected_counts()


def test_concurrent_identical_validation_failures_converge(workflow_db, tmp_path: Path) -> None:
    factory, _ids, _context, _task_id, service, principal, _run_id, request_id, arguments = (
        _validation_runtime(workflow_db, tmp_path)
    )

    def record(_index, barrier):
        wait_at_barrier(barrier, checkpoint="validation failure race ready")
        return service.record_replay_validation_failure(
            "create",
            arguments,
            principal=principal,
            request_id=str(request_id),
            error=_validation_error(),
        )

    results = run_concurrent_workers(2, record)
    replay_flags = [result["data"].get("request_replayed") for result in results]
    assert sorted(replay_flags, key=lambda value: value is True) == [None, True]
    assert without_replay_metadata(results[0]) == without_replay_metadata(results[1])
    with session_scope(factory) as session:
        assert _row_counts(session, request_id) == _expected_counts()


def test_validation_failure_conflict_preserves_first_outcome(workflow_db, tmp_path: Path) -> None:
    factory, _ids, _context, _task_id, service, principal, _run_id, request_id, arguments = (
        _validation_runtime(workflow_db, tmp_path)
    )
    first = service.record_replay_validation_failure(
        "create",
        arguments,
        principal=principal,
        request_id=str(request_id),
        error=_validation_error(),
    )
    with session_scope(factory) as session:
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        assert outcome is not None
        authoritative_payload = copy.deepcopy(outcome.result_payload)
        authoritative_sha256 = outcome.result_sha256

    conflicting_calls = (
        (arguments, _validation_error("task_id")),
        ({"operation_id": "different-invalid-value"}, _validation_error()),
    )
    for conflicting_arguments, conflicting_error in conflicting_calls:
        with pytest.raises(DishRuleError) as caught:
            service.record_replay_validation_failure(
                "create",
                conflicting_arguments,
                principal=principal,
                request_id=str(request_id),
                error=conflicting_error,
            )
        assert caught.value.code == "CONFLICT"
        assert caught.value.rule == "service_request_identity_conflict"

    with session_scope(factory) as session:
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        assert outcome is not None
        assert dict(outcome.result_payload) == authoritative_payload == first
        assert outcome.result_sha256 == authoritative_sha256
        assert _row_counts(session, request_id) == _expected_counts()
