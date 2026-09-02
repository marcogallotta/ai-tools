"""Native PostgreSQL validation-failure request/outcome replay evidence."""
from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from alembic import command as alembic_command
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.document_authority import parse_canonical_document, ready_document
from dish_pg.postgres_service import PostgresRuntimeService
from dish_pg.recovery_control import _clone_native_catalog, _clone_registry
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.repositories import AuthorityRepository
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from dish_pg.workflow import WorkflowAuthorityService, sha256_json
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from dish_tool.content_versions import content_identity
from tests.support.postgresql.concurrency import run_concurrent_workers, wait_at_barrier
from tests.support.canonical import TASK
from tests.support.postgresql.core import (
    NOW,
    _alembic_config,
    _bootstrap_registry,
    _next,
    core_db,
)
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import _register_run
from tests.support.postgresql.command import (
    _add_destination_section,
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _start_initial,
    _start_verification,
)

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


def _install_destructive_recovery_catalog_runtime(
    session, ids, context, *, authenticate_catalog: bool = True
):
    predecessor_generation_id = context["generation_id"]
    successor_generation_id = _next(ids)
    session.add(
        models.AuthorityGeneration(
            generation_id=successor_generation_id,
            predecessor_generation_id=predecessor_generation_id,
            creation_reason="destructive_restore",
            external_restore_control_id=f"restore:{successor_generation_id}",
            schema_head=ALEMBIC_HEAD,
            dish_release="dish-42619b9",
            status="pending",
            created_at=NOW,
            retired_at=None,
        )
    )
    session.flush()
    registry_version_id, _registry_activation_id = _clone_registry(
        session,
        predecessor_generation_id=predecessor_generation_id,
        generation_id=successor_generation_id,
        at=NOW,
        uuid_factory=lambda: _next(ids),
    )
    catalog_version_id, _catalog_activation_id = _clone_native_catalog(
        session,
        predecessor_generation_id=predecessor_generation_id,
        generation_id=successor_generation_id,
        at=NOW,
        uuid_factory=lambda: _next(ids),
    )
    predecessor_authority = session.scalar(
        select(models.AuthorityActivation).where(
            models.AuthorityActivation.generation_id == predecessor_generation_id,
            models.AuthorityActivation.outcome == "activated",
        )
    )
    activation = models.AuthorityActivation(
        activation_id=_next(ids),
        generation_id=successor_generation_id,
        import_run_id=context["import_run_id"],
        cutover_approval_id=f"restore:{successor_generation_id}",
        legacy_bundle_id=f"postgresql-restore:{successor_generation_id}",
        registry_version_id=registry_version_id,
        catalog_version_id=(
            catalog_version_id
            if authenticate_catalog
            else predecessor_authority.catalog_version_id
        ),
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
    AuthorityRepository(session).activate_generation(
        generation_id=successor_generation_id,
        activation=activation,
        at=NOW,
    )
    return {**context, "generation_id": successor_generation_id}


def _advance_post_burn_catalog_runtime(session, ids, context):
    predecessor = session.get(
        models.CurrentNativeCatalogRuntime, context["generation_id"]
    )
    assert predecessor is not None
    previous_attestation = session.get(
        models.NativeCatalogRuntimeAttestation, predecessor.attestation_id
    )
    previous_catalog = session.get(
        models.SectionCatalogVersion, predecessor.catalog_version_id
    )
    entries = list(
        session.scalars(
            select(models.SectionCatalogEntry).where(
                models.SectionCatalogEntry.catalog_version_id
                == predecessor.catalog_version_id
            )
        )
    )
    catalog_version_id = _next(ids)
    catalog_activation_id = _next(ids)
    attestation_id = _next(ids)
    revision = predecessor.attestation_revision + 1
    session.add(
        models.SectionCatalogVersion(
            catalog_version_id=catalog_version_id,
            generation_id=context["generation_id"],
            version_number=previous_catalog.version_number + 1,
            contract_binding_id=previous_catalog.contract_binding_id,
            catalog_sha256="d" * 64,
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW,
        )
    )
    session.flush()
    session.add_all(
        [
            models.SectionCatalogEntry(
                catalog_version_id=catalog_version_id,
                section_id=entry.section_id,
                ordinal=entry.ordinal,
                display_name=entry.display_name,
                workflow_role=entry.workflow_role,
            )
            for entry in entries
        ]
    )
    session.add(
        models.SectionCatalogActivation(
            catalog_activation_id=catalog_activation_id,
            generation_id=context["generation_id"],
            catalog_version_id=catalog_version_id,
            activation_route="recovery",
            import_run_id=None,
            command_execution_id=None,
            catalog_revision=revision,
            activated_at=NOW,
        )
    )
    session.flush()
    payload = {
        "contract": "native-section-runtime-attestation-v1",
        "generation_id": str(context["generation_id"]),
        "catalog_version_id": str(catalog_version_id),
        "catalog_activation_id": str(catalog_activation_id),
        "catalog_revision": revision,
        "authority_activation_id": None,
        "attestation_revision": revision,
    }
    session.add(
        models.NativeCatalogRuntimeAttestation(
            attestation_id=attestation_id,
            generation_id=context["generation_id"],
            catalog_version_id=catalog_version_id,
            catalog_activation_id=catalog_activation_id,
            predecessor_attestation_id=previous_attestation.attestation_id,
            authority_activation_id=None,
            attestation_revision=revision,
            attestation_sha256=sha256_json(payload),
            recorded_at=NOW,
        )
    )
    session.flush()
    active = session.get(models.ActiveSectionCatalog, context["generation_id"])
    active.catalog_version_id = catalog_version_id
    active.catalog_activation_id = catalog_activation_id
    active.catalog_revision = revision
    predecessor.attestation_id = attestation_id
    predecessor.catalog_version_id = catalog_version_id
    predecessor.catalog_activation_id = catalog_activation_id
    predecessor.attestation_revision = revision
    session.flush()
    return session.get(models.NativeCatalogRuntimeAttestation, attestation_id)


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


def test_native_runtime_health_rejects_stale_active_catalog_revision(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        _install_post_burn_catalog_runtime(session, ids, context)
        active = session.get(models.ActiveSectionCatalog, context["generation_id"])
        active.catalog_revision += 1

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


def test_native_runtime_health_accepts_burn_at_catalog_revision_greater_than_one(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        active = session.get(models.ActiveSectionCatalog, context["generation_id"])
        activation = session.get(
            models.SectionCatalogActivation, active.catalog_activation_id
        )
        active.catalog_revision = 2
        activation.catalog_revision = 2
        attestation, _current = _install_post_burn_catalog_runtime(
            session, ids, context
        )
        assert attestation.attestation_revision == 1
        assert active.catalog_revision == 2

    assert _health_runtime(factory, context).health()["ok"] is True


def test_native_runtime_health_accepts_destructive_recovery_cross_generation_edge(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        predecessor_attestation, _current = _install_post_burn_catalog_runtime(
            session, ids, context
        )
        successor_context = _install_destructive_recovery_catalog_runtime(
            session, ids, context
        )
        successor_current = session.get(
            models.CurrentNativeCatalogRuntime, successor_context["generation_id"]
        )
        successor_attestation = session.get(
            models.NativeCatalogRuntimeAttestation,
            successor_current.attestation_id,
        )
        successor_active = session.get(
            models.ActiveSectionCatalog, successor_context["generation_id"]
        )
        assert successor_attestation.predecessor_attestation_id == (
            predecessor_attestation.attestation_id
        )
        assert successor_attestation.attestation_revision == 2
        assert successor_active.catalog_revision == 1

    assert _health_runtime(factory, successor_context).health()["ok"] is True


def test_native_runtime_health_rejects_unauthenticated_recovery_catalog_edge(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        _install_post_burn_catalog_runtime(session, ids, context)
        successor_context = _install_destructive_recovery_catalog_runtime(
            session, ids, context, authenticate_catalog=False
        )

    _assert_catalog_runtime_unhealthy(
        _health_runtime(factory, successor_context).health()
    )


def test_native_runtime_health_accepts_adjacent_post_burn_catalog_successor(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        _install_post_burn_catalog_runtime(session, ids, context)
        _advance_post_burn_catalog_runtime(session, ids, context)

    assert _health_runtime(factory, context).health()["ok"] is True


def test_native_runtime_health_rejects_same_generation_catalog_revision_gap(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        _install_post_burn_catalog_runtime(session, ids, context)
        successor = _advance_post_burn_catalog_runtime(session, ids, context)
        active = session.get(models.ActiveSectionCatalog, context["generation_id"])
        activation = session.get(
            models.SectionCatalogActivation, successor.catalog_activation_id
        )
        active.catalog_revision = activation.catalog_revision = 3
        successor.attestation_sha256 = sha256_json(
            {
                "contract": "native-section-runtime-attestation-v1",
                "generation_id": str(context["generation_id"]),
                "catalog_version_id": str(successor.catalog_version_id),
                "catalog_activation_id": str(successor.catalog_activation_id),
                "catalog_revision": 3,
                "authority_activation_id": None,
                "attestation_revision": successor.attestation_revision,
            }
        )

    _assert_catalog_runtime_unhealthy(_health_runtime(factory, context).health())


def test_native_runtime_health_rejects_gapped_successor_chain(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        _install_post_burn_catalog_runtime(session, ids, context)
        successor = _advance_post_burn_catalog_runtime(session, ids, context)
        successor.attestation_revision = 3
        active = session.get(models.ActiveSectionCatalog, context["generation_id"])
        current = session.get(models.CurrentNativeCatalogRuntime, context["generation_id"])
        activation = session.get(
            models.SectionCatalogActivation, successor.catalog_activation_id
        )
        active.catalog_revision = current.attestation_revision = 3
        activation.catalog_revision = 3
        successor.attestation_sha256 = sha256_json(
            {
                "contract": "native-section-runtime-attestation-v1",
                "generation_id": str(context["generation_id"]),
                "catalog_version_id": str(successor.catalog_version_id),
                "catalog_activation_id": str(successor.catalog_activation_id),
                "catalog_revision": 3,
                "authority_activation_id": None,
                "attestation_revision": 3,
            }
        )

    _assert_catalog_runtime_unhealthy(_health_runtime(factory, context).health())


def test_native_runtime_health_rejects_forked_successor_chain(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        root, _current = _install_post_burn_catalog_runtime(session, ids, context)
        successor = _advance_post_burn_catalog_runtime(session, ids, context)
        fork = _advance_post_burn_catalog_runtime(session, ids, context)
        fork.predecessor_attestation_id = root.attestation_id
        active = session.get(models.ActiveSectionCatalog, context["generation_id"])
        current = session.get(models.CurrentNativeCatalogRuntime, context["generation_id"])
        active.catalog_version_id = successor.catalog_version_id
        active.catalog_activation_id = successor.catalog_activation_id
        active.catalog_revision = successor.attestation_revision
        current.attestation_id = successor.attestation_id
        current.catalog_version_id = successor.catalog_version_id
        current.catalog_activation_id = successor.catalog_activation_id
        current.attestation_revision = successor.attestation_revision

    _assert_catalog_runtime_unhealthy(_health_runtime(factory, context).health())


def test_native_post_burn_create_does_not_require_legacy_registry(core_db) -> None:
    factory, ids, context, _task_id = native_workflow_db(core_db)
    run_id = _next(ids)
    with session_scope(factory) as session:
        _install_post_burn_catalog_runtime(session, ids, context)
        session.delete(
            session.get(models.ActiveSectionRegistry, context["generation_id"])
        )
        session.flush()
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        result = _port(session, ids).execute(
            _call(
                "create",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"title": "Native post-burn dish"},
            )
        )
        assert result.ok, (result.code, result.data)
        state = session.get(
            models.DishState,
            (context["generation_id"], uuid.UUID(result.data["dish_id"])),
        )
        assert state.registry_version_id is None


def test_native_0044_to_0045_carries_ready_legacy_signoff_to_submit(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    author_run, verifier_run = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        destination_id = _add_destination_section(session, ids, context)
        destination_alias = session.scalar(
            select(models.SectionExternalAlias).where(
                models.SectionExternalAlias.section_id == destination_id
            )
        )
        session.delete(destination_alias)
        _register_run(
            session, generation_id=context["generation_id"], run_id=author_run
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="codex",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        prepared = port.execute(
            _call(
                "prepare",
                run_id=author_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "file_text": TASK,
                    "agent": "claude",
                    "model": "test-model",
                },
            )
        )
        assert prepared.ok, (prepared.code, prepared.data)
        _start_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        _inspect(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        reviewed = session.get(
            models.ContentVersion, uuid.UUID(prepared.data["content_version_id"])
        )
        approved = port.execute(
            _call(
                "approve",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "model": "test-verifier",
                    "correction": "none",
                    "reviewed_identity": reviewed.content_identity,
                    "semantic_review_complete": True,
                    "provenance_complete": True,
                },
            )
        )
        prior_signoff = session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.signed_content_version_id
                == uuid.UUID(approved.data["signed_content_version_id"])
            )
        )
        prior_signoff_id = prior_signoff.signoff_id
        source_content_id = prior_signoff.signed_content_version_id

        pending_parts = parse_canonical_document(
            file_text=TASK.replace("Sichuan — 12345", "Mediterranean — 77777")
        )
        pending_task_id = _next(ids)
        pending_task = CoreAuthorityService(
            session, uuid_factory=lambda: _next(ids)
        ).import_task_document(
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            contract_binding_id=context["binding_id"],
            spec=ImportedTaskSpec(
                task_id=pending_task_id,
                asana_task_gid="987654321",
                title=pending_parts.title,
                body=pending_parts.body,
                identity_scheme="dish-canonical-content-v1",
                content_identity=content_identity(
                    pending_parts.title, pending_parts.body
                ),
                project_ids=(context["project_id"],),
                section_id=context["section_id"],
                completed=False,
                observed_at=NOW,
            ),
        )
        pending_source_id = pending_task.content_version_id

        bootstrap_source = ready_document(
            pending_parts.document,
            agent="codex",
            model="test-model",
            at=NOW,
        )
        bootstrap_task_id = _next(ids)
        bootstrap_task = CoreAuthorityService(
            session, uuid_factory=lambda: _next(ids)
        ).import_task_document(
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            contract_binding_id=context["binding_id"],
            spec=ImportedTaskSpec(
                task_id=bootstrap_task_id,
                asana_task_gid="1217328963226164",
                title=bootstrap_source.title,
                body=bootstrap_source.body,
                identity_scheme="dish-canonical-content-v1",
                content_identity=content_identity(
                    bootstrap_source.title, bootstrap_source.body
                ),
                project_ids=(context["project_id"],),
                section_id=context["section_id"],
                completed=False,
                observed_at=NOW,
            ),
        )
        bootstrap_source_id = bootstrap_task.content_version_id

    dsn = factory.kw["bind"].url.render_as_string(hide_password=False)
    factory.kw["bind"].dispose()
    alembic_command.downgrade(
        _alembic_config(dsn), "0044_independent_archive"
    )
    alembic_command.upgrade(
        _alembic_config(dsn), "0045_native_section_authority"
    )

    with session_scope(factory) as session:
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert state.current_content_version_id != source_content_id
        transformed = session.get(
            models.ContentVersion, state.current_content_version_id
        )
        assert f"Destination section: Sichuan — section:{destination_id}" in transformed.body
        inherited = session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.signed_content_version_id
                == transformed.content_version_id
            )
        )
        assert inherited.signoff_kind == "inherited_non_material"
        assert inherited.inherited_from_signoff_id == prior_signoff_id
        pending_state = session.get(
            models.DishState,
            (context["generation_id"], pending_task_id),
        )
        assert pending_state.current_content_version_id != pending_source_id
        pending_transformed = session.get(
            models.ContentVersion, pending_state.current_content_version_id
        )
        pending_document = parse_canonical_document(
            title=pending_transformed.title, body=pending_transformed.body
        ).document
        assert pending_document.state.values["Status"] == "pending-verification"
        pending_destination = pending_document.planning_brief.values[
            "Destination section"
        ]
        assert pending_destination.startswith("Mediterranean — section:")
        pending_section_id = uuid.UUID(pending_destination.rsplit(":", 1)[1])
        assert session.get(models.Section, pending_section_id).logical_name == "Mediterranean"
        assert session.scalar(
            select(models.SectionExternalAlias).where(
                models.SectionExternalAlias.section_id == pending_section_id
            )
        ) is None
        assert session.get(
            models.SectionCatalogEntry,
            (context["catalog_version_id"], pending_section_id),
        ) is not None
        assert session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.signed_content_version_id
                == pending_transformed.content_version_id
            )
        ) is None
        bootstrap_state = session.get(
            models.DishState,
            (context["generation_id"], bootstrap_task_id),
        )
        assert bootstrap_state.current_content_version_id != bootstrap_source_id
        bootstrap_transformed = session.get(
            models.ContentVersion, bootstrap_state.current_content_version_id
        )
        bootstrap_document = parse_canonical_document(
            title=bootstrap_transformed.title, body=bootstrap_transformed.body
        ).document
        assert bootstrap_document.state.values["Status"] == "pending-verification"
        assert bootstrap_document.state.values["Verified by"] == "None"
        assert session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.signed_content_version_id
                == bootstrap_transformed.content_version_id
            )
        ) is None
        bootstrap_read = _port(session, ids).execute(
            _call(
                "read",
                run_id=_next(ids),
                arguments={"dish_id": str(bootstrap_task_id)},
            )
        )
        assert bootstrap_read.ok
        assert bootstrap_read.data["legal_actions"] == ()
        bootstrap_submit = _port(session, ids).execute(
            _call(
                "submit",
                run_id=author_run,
                request_id=_next(ids),
                arguments={"task_id": str(bootstrap_task_id)},
            )
        )
        assert bootstrap_submit.ok is False
        submitted = _port(session, ids).execute(
            _call(
                "submit",
                run_id=author_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                },
            )
        )
        assert submitted.ok, (submitted.code, submitted.data)
        assert submitted.data["destination_section_id"] == str(destination_id)


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
