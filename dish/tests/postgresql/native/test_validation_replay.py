"""Native PostgreSQL validation-failure request/outcome replay evidence."""
from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.postgres_service import PostgresRuntimeService
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.workflow import WorkflowAuthorityService, sha256_json
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from tests.support.postgresql.concurrency import run_concurrent_workers, wait_at_barrier
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next, core_db
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _runtime(factory) -> PostgresRuntimeService:
    runtime = PostgresRuntimeService.__new__(PostgresRuntimeService)
    runtime._session_maker = factory
    runtime._cursor_secret = b"native-validation-replay-secret!"
    return runtime


def _health_runtime(factory, context) -> PostgresRuntimeService:
    runtime = _runtime(factory)
    runtime.config = SimpleNamespace(
        bind_host="127.0.0.1",
        action_bind_host="127.0.0.1",
    )
    runtime._expected_database = str(factory.kw["bind"].url.database)
    runtime._expected_schema_head = ALEMBIC_HEAD
    runtime._expected_release = "dish-42619b9"
    runtime._expected_generation_id = context["generation_id"]
    runtime._profile = "test"
    return runtime


def _install_post_burn_authority(session, ids, context):
    active = session.get(models.ActiveSectionCatalog, context["generation_id"])
    assert active is not None
    activation_id = _next(ids)
    session.add(
        models.AuthorityActivation(
            activation_id=activation_id,
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            cutover_approval_id="native-runtime-health",
            legacy_bundle_id="native-runtime-health",
            registry_version_id=context["registry_version_id"],
            catalog_version_id=active.catalog_version_id,
            honest_binding_id=context["binding_id"],
            rehearsal_id=None,
            schema_head=ALEMBIC_HEAD,
            dish_release="dish-42619b9",
            honest_release="honest-1",
            protocol_release="protocol-1",
            openapi_release="openapi-1",
            routing_release="route-1",
            projection_epoch=_next(ids),
            outcome="activated",
            rollback_burned_at=NOW,
            recorded_at=NOW,
        )
    )
    session.flush()
    return active, activation_id


def _install_post_burn_catalog_runtime(session, ids, context):
    active, activation_id = _install_post_burn_authority(session, ids, context)
    attestation_id = _next(ids)
    attestation_payload = {
        "contract": "native-section-runtime-attestation-v1",
        "generation_id": str(context["generation_id"]),
        "catalog_version_id": str(active.catalog_version_id),
        "catalog_activation_id": str(active.catalog_activation_id),
        "catalog_revision": active.catalog_revision,
        "authority_activation_id": str(activation_id),
        "attestation_revision": 1,
    }
    attestation = models.NativeCatalogRuntimeAttestation(
        attestation_id=attestation_id,
        generation_id=context["generation_id"],
        catalog_version_id=active.catalog_version_id,
        catalog_activation_id=active.catalog_activation_id,
        predecessor_attestation_id=None,
        authority_activation_id=activation_id,
        attestation_revision=1,
        attestation_sha256=sha256_json(attestation_payload),
        recorded_at=NOW,
    )
    session.add(attestation)
    session.flush()
    current = models.CurrentNativeCatalogRuntime(
        generation_id=context["generation_id"],
        attestation_id=attestation_id,
        catalog_version_id=active.catalog_version_id,
        catalog_activation_id=active.catalog_activation_id,
        attestation_revision=1,
        updated_at=NOW,
    )
    session.add(current)
    session.flush()
    return attestation, current


def _assert_catalog_runtime_unhealthy(result) -> None:
    assert result == {
        "ok": False,
        "startup_ready": False,
        "backend": "postgresql",
        "profile": "test",
        "pid": result["pid"],
        "code": "BACKEND_REJECTED",
        "rule": "postgresql_native_catalog_runtime_unhealthy",
        "retryable": False,
    }


def test_native_runtime_health_preserves_valid_pre_burn_identity(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )

    health = _health_runtime(factory, context).health()

    assert health["ok"] is True
    assert health["startup_ready"] is True
    assert health["identity"]["generation_id"] == str(context["generation_id"])


def test_native_runtime_health_rejects_missing_post_burn_catalog_runtime(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )
        _install_post_burn_authority(session, ids, context)

    _assert_catalog_runtime_unhealthy(_health_runtime(factory, context).health())


@pytest.mark.parametrize("corruption", ["stale_revision", "mismatched_attestation"])
def test_native_runtime_health_rejects_stale_or_mismatched_catalog_lineage(
    core_db, corruption
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )
        attestation, current = _install_post_burn_catalog_runtime(
            session, ids, context
        )
        if corruption == "stale_revision":
            current.attestation_revision = 2
        else:
            attestation.attestation_sha256 = "f" * 64

    _assert_catalog_runtime_unhealthy(_health_runtime(factory, context).health())


def test_native_runtime_health_accepts_current_post_burn_catalog(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )
        _install_post_burn_catalog_runtime(session, ids, context)

    assert _health_runtime(factory, context).health()["ok"] is True


def _error(field: str = "operation_id") -> DishRuleError:
    return DishRuleError(
        "INVALID_ARGUMENT",
        f"{field} must be a canonical UUID",
        rule="uuid_identifier_required",
        details={"field": field},
    )


def _count(session, model, request_id) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(model).where(model.request_id == request_id)
        )
        or 0
    )


def _assert_one_validation_outcome(factory, request_id) -> None:
    with session_scope(factory) as session:
        assert _count(session, wf.ServiceRequest, request_id) == 1
        assert _count(session, wf.ServiceRequestOutcome, request_id) == 1
        assert _count(session, wf.CommandExecution, request_id) == 0
        assert _count(session, wf.GovernedAuditEvent, request_id) == 1
        assert _count(session, wf.InvocationAuditObligation, request_id) == 1


def _without_replay_metadata(payload):
    normalized = copy.deepcopy(payload)
    normalized["data"].pop("request_replayed", None)
    return normalized


def test_native_validation_failure_replays_one_authoritative_outcome(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        run_id = _next(ids)
        request_id = _next(ids)
        WorkflowAuthorityService(session).register_run(
            run_id=run_id,
            generation_id=context["generation_id"],
            owner_id="owner-1",
            agent="claude",
            capability_digest=run_id.bytes + run_id.bytes,
            registered_at=NOW,
        )

    runtime = _runtime(factory)
    principal = ServicePrincipal.from_values("owner-1", str(run_id))
    arguments = {"operation_id": "not-a-uuid"}
    first = runtime.record_replay_validation_failure(
        "create",
        arguments,
        principal=principal,
        request_id=str(request_id),
        error=_error(),
    )
    replay = runtime.record_replay_validation_failure(
        "create",
        arguments,
        principal=principal,
        request_id=str(request_id),
        error=_error(),
    )

    assert "request_replayed" not in first["data"]
    assert replay["data"]["request_replayed"] is True
    assert _without_replay_metadata(replay) == first
    _assert_one_validation_outcome(factory, request_id)

    with session_scope(factory) as session:
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        assert outcome is not None
        authoritative_payload = copy.deepcopy(outcome.result_payload)
        authoritative_sha256 = outcome.result_sha256

    with pytest.raises(DishRuleError) as caught:
        runtime.record_replay_validation_failure(
            "create",
            arguments,
            principal=principal,
            request_id=str(request_id),
            error=_error("task_id"),
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
        assert dict(outcome.result_payload) == authoritative_payload
        assert outcome.result_sha256 == authoritative_sha256
    _assert_one_validation_outcome(factory, request_id)


def test_native_concurrent_identical_validation_failures_converge(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        run_id = _next(ids)
        request_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)

    runtime = _runtime(factory)
    principal = ServicePrincipal.from_values("owner-1", str(run_id))
    arguments = {"operation_id": "not-a-uuid"}

    def record(_index, barrier):
        wait_at_barrier(barrier, checkpoint="native validation replay race")
        return runtime.record_replay_validation_failure(
            "create",
            arguments,
            principal=principal,
            request_id=str(request_id),
            error=_error(),
        )

    results = run_concurrent_workers(2, record)
    replay_flags = [result["data"].get("request_replayed") for result in results]
    assert sorted(replay_flags, key=lambda value: value is True) == [None, True]
    assert _without_replay_metadata(results[0]) == _without_replay_metadata(results[1])
    _assert_one_validation_outcome(factory, request_id)


def test_native_closed_admission_preserves_first_request_reservation(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
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
        reserved_payload = {"command": "start", "arguments": {"task_id": str(task_id)}}
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

    runtime = _runtime(factory)
    result = runtime.record_replay_validation_failure(
        "create",
        {"operation_id": "not-a-uuid"},
        principal=ServicePrincipal.from_values("owner-1", str(run_id)),
        request_id=str(request_id),
        error=_error(),
    )
    assert result["code"] == "INVALID_ARGUMENT"

    with session_scope(factory) as session:
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        reservation = session.get(reservations.FirstRequestReservation, reservation_id)
        assert control is not None and control.state == "closed"
        assert reservation is not None
        assert reservation.state == "reserved"
        assert reservation.reservation_revision == 1
        assert reservation.consumed_at is None
    _assert_one_validation_outcome(factory, request_id)
