from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.repositories import (
    CatalogRepository,
    CoreAuthorityError,
    DishRepository,
    ScalarMutationSource,
)
from dish_pg.workflow import StaleAuthorityError, WorkflowAuthorityService
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import (
    NOW,
    _admit,
    _execution,
    _next,
    _register_run,
    workflow_db,
)


def _activate_native_runtime(
    session,
    ids: Iterator[uuid.UUID],
    context: dict[str, uuid.UUID],
    *,
    role_overrides: dict[uuid.UUID, str] | None = None,
) -> dict[str, uuid.UUID]:
    active_registry = session.get(models.ActiveSectionRegistry, context["generation_id"])
    assert active_registry is not None
    legacy_entries = tuple(
        session.scalars(
            select(models.SectionRegistryEntry)
            .where(
                models.SectionRegistryEntry.registry_version_id
                == active_registry.registry_version_id
            )
            .order_by(models.SectionRegistryEntry.ordinal)
        )
    )
    assert legacy_entries
    for entry in legacy_entries:
        if session.get(models.Section, entry.section_id) is None:
            session.add(
                models.Section(
                    section_id=entry.section_id,
                    logical_name=f"native-{entry.section_id}",
                    lifecycle="active",
                    created_at=NOW,
                    retired_at=None,
                )
            )
    session.flush()

    catalog_version_id = _next(ids)
    catalog_activation_id = _next(ids)
    contract = CatalogRepository(session).install_catalog_revision(
        version=models.SectionCatalogVersion(
            catalog_version_id=catalog_version_id,
            generation_id=context["generation_id"],
            version_number=1,
            contract_binding_id=context["binding_id"],
            catalog_sha256="a" * 64,
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW,
        ),
        entries=[
            models.SectionCatalogEntry(
                catalog_version_id=catalog_version_id,
                section_id=entry.section_id,
                ordinal=entry.ordinal,
                display_name=entry.display_name,
                workflow_role=(role_overrides or {}).get(
                    entry.section_id, entry.workflow_role
                ),
            )
            for entry in legacy_entries
        ],
        activation=models.SectionCatalogActivation(
            catalog_activation_id=catalog_activation_id,
            generation_id=context["generation_id"],
            catalog_version_id=catalog_version_id,
            activation_route="recovery",
            import_run_id=None,
            command_execution_id=None,
            catalog_revision=1,
            activated_at=NOW,
        ),
        expected_catalog_version_id=None,
        expected_catalog_activation_id=None,
        expected_catalog_revision=None,
    )

    migration_event_id = _next(ids)
    session.add(
        models.AppliedMigrationEvent(
            migration_event_id=migration_event_id,
            generation_id=context["generation_id"],
            revision="test_native_runtime_switch",
            predecessor_revision="0049_native_catalog_runtime_authority_root",
            migration_code_sha256="b" * 64,
            dish_release="dish-42619b9",
            initiator="test",
            outcome="applied",
            started_at=NOW,
            terminal_at=NOW,
            details={"source_commit_sha": "c" * 40},
        )
    )
    attestation_id = _next(ids)
    session.add(
        models.NativeCatalogRuntimeAttestation(
            attestation_id=attestation_id,
            generation_id=context["generation_id"],
            catalog_version_id=catalog_version_id,
            catalog_activation_id=catalog_activation_id,
            predecessor_attestation_id=None,
            baseline_migration_event_id=migration_event_id,
            attestation_revision=1,
            attestation_sha256=models.compute_attestation_sha256(
                generation_id=context["generation_id"],
                catalog_version_id=catalog_version_id,
                catalog_activation_id=catalog_activation_id,
                contract_binding_id=context["binding_id"],
                attestation_revision=1,
                predecessor_attestation_id=None,
                baseline_migration_event_id=migration_event_id,
                baseline_revision="test_native_runtime_switch",
                baseline_migration_code_sha256="b" * 64,
                baseline_dish_release="dish-42619b9",
                baseline_source_commit_sha="c" * 40,
            ),
            recorded_at=NOW,
        )
    )
    session.flush()
    session.add(
        models.CurrentNativeCatalogRuntime(
            generation_id=context["generation_id"],
            attestation_id=attestation_id,
            catalog_version_id=catalog_version_id,
            catalog_activation_id=catalog_activation_id,
            attestation_revision=1,
            updated_at=NOW,
        )
    )

    for state in session.scalars(
        select(models.DishState).where(
            models.DishState.generation_id == context["generation_id"]
        )
    ):
        next_dish_version = state.dish_version + 1
        session.add(
            models.DishMutationReceipt(
                generation_id=state.generation_id,
                task_id=state.task_id,
                dish_version=next_dish_version,
                source_route="import",
                import_run_id=context["import_run_id"],
                command_execution_id=None,
                content_changed=False,
                placement_changed=True,
                completion_changed=False,
                archive_changed=False,
                occurred_at=NOW,
            )
        )
        session.flush()
        state.catalog_version_id = catalog_version_id
        state.dish_version = next_dish_version
        state.placement_version = next_dish_version
        state.updated_at = NOW
        session.flush()

    return {
        "catalog_version_id": contract.catalog_version.catalog_version_id,
        "catalog_activation_id": contract.catalog_activation.catalog_activation_id,
        "attestation_id": attestation_id,
    }


def test_native_task_and_scalar_fences_use_catalog_and_placement_not_membership(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        native = _activate_native_runtime(session, ids, context)
        run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session)
        _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        _execution(
            service,
            execution_id=execution_id,
            request_id=request_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
        )
        fence = service.repo.capture_task_fence(
            execution_id=execution_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )
        state = session.get(models.DishState, (context["generation_id"], task_id))
        membership = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        assert state is not None and membership is not None
        assert fence.expected_placement_version == state.placement_version
        assert fence.catalog_version_id == native["catalog_version_id"]

        membership.membership_revision += 1
        session.flush()
        assert service.repo.assert_task_fence(execution_id).task_id == task_id

        mutation = DishRepository(session).begin_scalar_mutation(
            generation_id=context["generation_id"],
            task_id=task_id,
            expected_dish_version=state.dish_version,
            expected_membership_revision=999999,
            expected_placement_version=state.placement_version,
            catalog_version_id=native["catalog_version_id"],
            source=ScalarMutationSource(
                route="import",
                import_run_id=context["import_run_id"],
                occurred_at=NOW,
            ),
        )
        mutation.place(
            section_id=state.section_id,
            catalog_version_id=native["catalog_version_id"],
        )
        mutation.finalize()
        with pytest.raises(StaleAuthorityError, match="task fence"):
            service.repo.assert_task_fence(execution_id)

        current = session.get(models.DishState, (context["generation_id"], task_id))
        assert current is not None
        with pytest.raises(CoreAuthorityError, match="authority domain is stale"):
            DishRepository(session).begin_scalar_mutation(
                generation_id=context["generation_id"],
                task_id=task_id,
                expected_dish_version=current.dish_version,
                expected_membership_revision=membership.membership_revision,
                source=ScalarMutationSource(
                    route="import",
                    import_run_id=context["import_run_id"],
                    occurred_at=NOW,
                ),
            )


def test_native_operation_and_inspection_carry_exact_catalog_identity(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        native = _activate_native_runtime(session, ids, context)
        author_run, verifier_run = _next(ids), _next(ids)
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
        operation_id = uuid.UUID(started.data["operation_id"])
        operation = session.get(wf.WorkflowOperation, operation_id)
        assert operation is not None
        assert operation.catalog_version_id == native["catalog_version_id"]

        prepared = _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=str(operation_id),
            run_id=author_run,
        )
        assert prepared.ok
        cycle_id = uuid.UUID(prepared.data["cycle_id"])
        started_verification = _start_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=str(operation_id),
            run_id=verifier_run,
            target_operation_id=operation_id,
            target_cycle_id=cycle_id,
        )
        assert started_verification.ok
        inspected = _inspect(
            port,
            ids,
            task_id=task_id,
            operation_id=str(operation_id),
            run_id=verifier_run,
        )
        inspection = session.get(
            wf.VerificationInspectionOccurrence,
            uuid.UUID(inspected.data["inspection_id"]),
        )
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert inspection is not None and state is not None
        assert inspection.catalog_version_id == native["catalog_version_id"]
        assert inspection.placement_version == state.placement_version
        assert state.catalog_version_id == native["catalog_version_id"]


def test_native_create_resolves_research_role_from_catalog_without_project_membership(
    workflow_db,
) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        verification_section_id = _add_verification_queue(session, ids, context)
        native = _activate_native_runtime(
            session,
            ids,
            context,
            role_overrides={
                context["section_id"]: "verification_queue",
                verification_section_id: "research_queue",
            },
        )
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "create",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"title": "Native role dish"},
            )
        )
        assert result.ok, (result.code, result.http_status, result.data)
        task_id = uuid.UUID(result.data["task_id"])
        state = session.get(models.DishState, (context["generation_id"], task_id))
        membership = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        assert state is not None and membership is not None
        assert state.section_id == verification_section_id
        assert state.catalog_version_id == native["catalog_version_id"]
        assert membership.membership_revision == 0
        assert session.scalar(
            select(func.count())
            .select_from(models.CurrentTaskProjectMembership)
            .where(models.CurrentTaskProjectMembership.task_id == task_id)
        ) == 0


def test_native_mutation_fails_closed_if_active_catalog_outpaces_runtime_pointer(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        native = _activate_native_runtime(session, ids, context)
        current = CatalogRepository(session).active_catalog_contract(
            context["generation_id"]
        )
        next_catalog_version_id = _next(ids)
        CatalogRepository(session).install_catalog_revision(
            version=models.SectionCatalogVersion(
                catalog_version_id=next_catalog_version_id,
                generation_id=context["generation_id"],
                version_number=current.catalog_version.version_number + 1,
                contract_binding_id=context["binding_id"],
                catalog_sha256="d" * 64,
                source_registry_version_id=None,
                transform_sha256=None,
                created_at=NOW,
            ),
            entries=[
                models.SectionCatalogEntry(
                    catalog_version_id=next_catalog_version_id,
                    section_id=entry.section_id,
                    ordinal=entry.ordinal,
                    display_name=entry.display_name,
                    workflow_role=entry.workflow_role,
                )
                for entry in current.entries
            ],
            activation=models.SectionCatalogActivation(
                catalog_activation_id=_next(ids),
                generation_id=context["generation_id"],
                catalog_version_id=next_catalog_version_id,
                activation_route="recovery",
                import_run_id=None,
                command_execution_id=None,
                catalog_revision=current.catalog_activation.catalog_revision + 1,
                activated_at=NOW,
            ),
            expected_catalog_version_id=current.catalog_version.catalog_version_id,
            expected_catalog_activation_id=current.catalog_activation.catalog_activation_id,
            expected_catalog_revision=current.catalog_activation.catalog_revision,
        )

        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert state is not None
        with pytest.raises(
            CoreAuthorityError, match="exact active native catalog"
        ):
            DishRepository(session).begin_scalar_mutation(
                generation_id=context["generation_id"],
                task_id=task_id,
                expected_dish_version=state.dish_version,
                expected_membership_revision=0,
                expected_placement_version=state.placement_version,
                catalog_version_id=native["catalog_version_id"],
                source=ScalarMutationSource(
                    route="import",
                    import_run_id=context["import_run_id"],
                    occurred_at=NOW,
                ),
            )


def test_pre_switch_operation_cannot_cross_native_runtime_switch(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id, request_id, execution_id, operation_id = (
            _next(ids),
            _next(ids),
            _next(ids),
            _next(ids),
        )
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session)
        _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        _execution(
            service,
            execution_id=execution_id,
            request_id=request_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
        )
        service.repo.capture_task_fence(
            execution_id=execution_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )
        operation = service.create_operation(
            operation_id=operation_id,
            execution_id=execution_id,
            task_id=task_id,
            kind="initial",
            phase="prepare_required",
            persisted_actions=["prepare"],
            created_at=NOW,
        )
        assert operation.catalog_version_id is None

        _activate_native_runtime(session, ids, context)
        new_run, new_request, new_execution = _next(ids), _next(ids), _next(ids)
        _register_run(
            session, generation_id=context["generation_id"], run_id=new_run
        )
        _admit(
            service,
            request_id=new_request,
            generation_id=context["generation_id"],
            run_id=new_run,
        )
        _execution(
            service,
            execution_id=new_execution,
            request_id=new_request,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
        )
        service.repo.capture_task_fence(
            execution_id=new_execution,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )
        with pytest.raises(StaleAuthorityError, match="operation catalog authority"):
            service.repo.capture_operation_fence(
                execution_id=new_execution,
                operation_id=operation_id,
                at=NOW,
            )
