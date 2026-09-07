from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.native_catalog_runtime_finalizer import (
    FINALIZER_REVISION,
    NativeCatalogRuntimeFinalizerError,
    finalize_native_catalog_runtime_authority,
)
from dish_pg.native_section_carry_forward import (
    RepositoryIdentity,
    apply_carry_forward,
    build_carry_forward_plan,
)
from dish_pg.native_section_content_materializer import (
    NativeSectionContentMaterializationError,
    materialize_staged_native_section_content,
    materialized_content_version_id,
)
from dish_pg.workflow import WorkflowAuthorityService
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from tests.postgresql.test_native_section_content_carry_forward import (
    NOW,
    SOURCE_COMMIT,
    SOURCE_TREE,
    _fixture,
)
from tests.support.postgresql.core import _next
from tests.support.postgresql.workflow import _admit, _execution, _register_run

pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


@pytest.fixture(autouse=True)
def _verified_repository_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        "dish_pg.native_section_carry_forward._verified_repository_identity",
        lambda: RepositoryIdentity(commit_sha=SOURCE_COMMIT, tree_sha=SOURCE_TREE),
    )


def _stage_pr3(session: Session, ids: Iterator[uuid.UUID], **fixture_kwargs):
    seeded, expectation, source_rows = _fixture(session, ids, **fixture_kwargs)
    plan = build_carry_forward_plan(session, expectation=expectation)
    receipt = apply_carry_forward(
        session,
        expected_snapshot_sha256=plan.source_snapshot_sha256,
        source_commit=SOURCE_COMMIT,
        expectation=expectation,
        now=NOW,
    )
    occurrences = tuple(
        session.scalars(
            select(models.NativeSectionContentCarryForwardOccurrence).order_by(
                models.NativeSectionContentCarryForwardOccurrence.task_id
            )
        )
    )
    return seeded, expectation, source_rows, uuid.UUID(receipt["migration_event_id"]), occurrences


def _complete_after_staging(
    session: Session, ids: Iterator[uuid.UUID], seeded, task_id: uuid.UUID
) -> int:
    state = session.get(models.DishState, (seeded["generation_id"], task_id))
    assert state is not None
    run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
    _register_run(session, generation_id=seeded["generation_id"], run_id=run_id)
    workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
    _admit(
        workflow,
        request_id=request_id,
        generation_id=seeded["generation_id"],
        run_id=run_id,
        command="cooked",
        payload={"dish_id": str(task_id)},
    )
    _execution(
        workflow,
        execution_id=execution_id,
        request_id=request_id,
        generation_id=seeded["generation_id"],
        task_id=task_id,
        binding_id=seeded["binding_id"],
        command="cooked",
    )
    next_version = state.dish_version + 1
    session.add(
        models.DishMutationReceipt(
            generation_id=seeded["generation_id"],
            task_id=task_id,
            dish_version=next_version,
            source_route="command_execution",
            import_run_id=None,
            command_execution_id=execution_id,
            content_changed=False,
            placement_changed=False,
            completion_changed=True,
            archive_changed=False,
            occurred_at=NOW + timedelta(minutes=1),
        )
    )
    session.flush()
    changed = session.execute(
        update(models.DishState)
        .where(
            models.DishState.generation_id == seeded["generation_id"],
            models.DishState.task_id == task_id,
            models.DishState.dish_version == state.dish_version,
        )
        .values(
            completed=True,
            completion_reason="cooked",
            dish_version=next_version,
            completion_version=next_version,
            updated_at=NOW + timedelta(minutes=1),
        )
        .execution_options(synchronize_session=False)
    )
    assert changed.rowcount == 1
    session.flush()
    return next_version


def test_materializes_staged_successors_inside_caller_transaction(core_db) -> None:
    factory, ids = core_db
    with factory() as session:
        with session.begin():
            seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(session, ids)
            before = {
                row.task_id: (state.section_id, state.registry_version_id, state.completion_version)
                for row in occurrences
                if (state := session.get(models.DishState, (seeded["generation_id"], row.task_id)))
                is not None
            }

            result = materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
            )
            assert session.in_transaction()
            assert (result.occurrence_count, result.materialized_count, result.already_materialized_count) == (23, 23, 0)

            for occurrence in occurrences:
                state = session.get(models.DishState, (seeded["generation_id"], occurrence.task_id))
                successor_id = materialized_content_version_id(occurrence.carry_forward_id)
                successor = session.get(models.ContentVersion, successor_id)
                assert state is not None and successor is not None
                assert successor.predecessor_content_version_id == occurrence.source_content_version_id
                assert successor.body == occurrence.transformed_body
                assert successor.content_identity == occurrence.transformed_content_identity
                assert successor.created_dish_version == occurrence.source_dish_version + 1
                assert state.current_content_version_id == successor_id
                assert state.catalog_version_id == expectation.base_catalog_version_id
                assert state.dish_version == state.placement_version == occurrence.source_dish_version + 1
                assert (state.section_id, state.registry_version_id, state.completion_version) == before[occurrence.task_id]
                mutation = session.get(
                    models.DishMutationReceipt,
                    (seeded["generation_id"], occurrence.task_id, occurrence.source_dish_version + 1),
                )
                assert mutation is not None
                assert (mutation.content_changed, mutation.placement_changed) == (True, True)
                assert (mutation.completion_changed, mutation.archive_changed) == (False, False)

            retry = materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
            )
            assert (retry.materialized_count, retry.already_materialized_count) == (0, 23)


def test_materializes_when_current_content_predates_source_dish_version(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, source_rows = _fixture(session, ids)
        task_id, content_version_id = source_rows[0]
        state = session.get(models.DishState, (seeded["generation_id"], task_id))
        source = session.get(models.ContentVersion, content_version_id)
        assert state is not None and source is not None
        assert state.dish_version == source.created_dish_version == 1

        advanced_version = state.dish_version + 1
        session.add(
            models.DishMutationReceipt(
                generation_id=seeded["generation_id"],
                task_id=task_id,
                dish_version=advanced_version,
                source_route="import",
                import_run_id=seeded["import_run_id"],
                command_execution_id=None,
                content_changed=False,
                placement_changed=False,
                completion_changed=True,
                archive_changed=False,
                occurred_at=NOW,
            )
        )
        session.flush()
        advanced = session.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == seeded["generation_id"],
                models.DishState.task_id == task_id,
                models.DishState.dish_version == 1,
            )
            .values(
                completed=True,
                completion_reason="imported",
                dish_version=advanced_version,
                completion_version=advanced_version,
                updated_at=NOW,
            )
            .execution_options(synchronize_session=False)
        )
        assert advanced.rowcount == 1
        session.flush()
        session.expire(state)
        assert state.current_content_version_id == content_version_id
        assert state.dish_version == advanced_version
        assert source.created_dish_version < state.dish_version

        plan = build_carry_forward_plan(session, expectation=expectation)
        receipt = apply_carry_forward(
            session,
            expected_snapshot_sha256=plan.source_snapshot_sha256,
            source_commit=SOURCE_COMMIT,
            expectation=expectation,
            now=NOW,
        )
        migration_event_id = uuid.UUID(receipt["migration_event_id"])
        occurrence = session.scalar(
            select(models.NativeSectionContentCarryForwardOccurrence).where(
                models.NativeSectionContentCarryForwardOccurrence.task_id == task_id
            )
        )
        assert occurrence is not None
        assert occurrence.source_content_version_id == content_version_id
        assert occurrence.source_dish_version == advanced_version
        assert source.created_dish_version < occurrence.source_dish_version

        result = materialize_staged_native_section_content(
            session,
            generation_id=seeded["generation_id"],
            migration_event_id=migration_event_id,
            catalog_version_id=expectation.base_catalog_version_id,
            materialized_at=NOW,
        )
        assert (result.materialized_count, result.already_materialized_count) == (23, 0)
        successor_id = materialized_content_version_id(occurrence.carry_forward_id)
        successor = session.get(models.ContentVersion, successor_id)
        session.expire(state)
        assert successor is not None
        assert successor.predecessor_content_version_id == content_version_id
        assert successor.created_dish_version == advanced_version + 1
        assert state.current_content_version_id == successor_id
        assert state.dish_version == advanced_version + 1
        assert state.completion_version == advanced_version


@pytest.mark.parametrize(
    "corruption",
    ("unknown_scheme", "stored_identity", "source_bytes"),
)
def test_historical_imported_identity_scheme_still_rejects_corruption(
    core_db, corruption
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        fixture_kwargs = {"first_identity_scheme": "2"}
        if corruption == "unknown_scheme":
            fixture_kwargs["first_identity_scheme"] = "3"
        elif corruption == "stored_identity":
            fixture_kwargs["first_content_identity"] = "0" * 64
        else:
            fixture_kwargs["first_title_suffix_after_identity"] = " corrupted"
        seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(
            session, ids, **fixture_kwargs
        )
        occurrence = occurrences[0]

        with pytest.raises(
            NativeSectionContentMaterializationError,
            match="source occurrence no longer matches immutable source content",
        ):
            materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
            )


def test_finalizer_rebases_after_committed_completion_only_command(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, _event_id, occurrences = _stage_pr3(session, ids)
        occurrence = occurrences[0]
        task_id = occurrence.task_id
        source_content_id = occurrence.source_content_version_id
        successor_id = materialized_content_version_id(occurrence.carry_forward_id)
        assert occurrence.source_dish_version == 1

    with session_scope(factory) as session:
        assert _complete_after_staging(session, ids, seeded, task_id) == 2
        state = session.get(models.DishState, (seeded["generation_id"], task_id))
        assert state is not None
        assert state.current_content_version_id == source_content_id
        assert state.dish_version == state.completion_version == 2
        assert state.completed is True

    with session_scope(factory) as session:
        result = finalize_native_catalog_runtime_authority(
            session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
        )
        state = session.get(models.DishState, (seeded["generation_id"], task_id))
        successor = session.get(models.ContentVersion, successor_id)
        resolved = models.resolve_current_native_catalog_runtime(
            session, seeded["generation_id"]
        )
        assert result.inserted is True and resolved is not None
        assert successor is not None and successor.created_dish_version == 3
        assert state is not None
        assert state.dish_version == state.placement_version == 3
        assert state.completion_version == 2 and state.completed is True
        assert state.current_content_version_id == successor_id
        assert state.catalog_version_id == expectation.base_catalog_version_id

    with session_scope(factory) as session:
        retry = finalize_native_catalog_runtime_authority(
            session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=2)
        )
        assert retry.inserted is False
        assert retry.materialization.materialized_count == 0
        assert retry.materialization.already_materialized_count == 23


def test_rebased_finalizer_failure_rolls_back_all_new_authority(
    core_db, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, _expectation, _, _event_id, occurrences = _stage_pr3(session, ids)
        source_content_ids = {
            occurrence.task_id: occurrence.source_content_version_id
            for occurrence in occurrences
        }
        rebased = occurrences[0]
        rebased_task_id = rebased.task_id
        successor_id = materialized_content_version_id(rebased.carry_forward_id)

    with session_scope(factory) as session:
        assert _complete_after_staging(session, ids, seeded, rebased_task_id) == 2

    def _fail_after_materialization(*args, **kwargs):
        materialize_staged_native_section_content(*args, **kwargs)
        raise NativeSectionContentMaterializationError(
            "injected failure after rebased staged materialization"
        )

    monkeypatch.setattr(
        "dish_pg.native_catalog_runtime_finalizer.materialize_staged_native_section_content",
        _fail_after_materialization,
    )
    with pytest.raises(
        NativeCatalogRuntimeFinalizerError,
        match="injected failure after rebased staged materialization",
    ):
        with session_scope(factory) as session:
            finalize_native_catalog_runtime_authority(
                session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
            )

    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(models.AppliedMigrationEvent).where(
                models.AppliedMigrationEvent.generation_id == seeded["generation_id"],
                models.AppliedMigrationEvent.revision == FINALIZER_REVISION,
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(models.NativeCatalogRuntimeAttestation).where(
                models.NativeCatalogRuntimeAttestation.generation_id == seeded["generation_id"]
            )
        ) == 0
        assert session.get(models.CurrentNativeCatalogRuntime, seeded["generation_id"]) is None
        assert session.get(models.ContentVersion, successor_id) is None
        assert session.get(
            models.DishMutationReceipt, (seeded["generation_id"], rebased_task_id, 3)
        ) is None
        for occurrence in occurrences:
            state = session.get(
                models.DishState, (seeded["generation_id"], occurrence.task_id)
            )
            assert state is not None
            assert state.current_content_version_id == source_content_ids[occurrence.task_id]
            assert state.catalog_version_id is None
        state = session.get(models.DishState, (seeded["generation_id"], rebased_task_id))
        assert state is not None
        assert state.dish_version == state.completion_version == 2
        assert state.completed is True


def test_rejects_intervening_placement_receipt_before_materialization(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(session, ids)
        occurrence = occurrences[0]
        state = session.get(models.DishState, (seeded["generation_id"], occurrence.task_id))
        assert state is not None and occurrence.source_dish_version == state.dish_version == 1
        session.add(
            models.DishMutationReceipt(
                generation_id=seeded["generation_id"],
                task_id=occurrence.task_id,
                dish_version=2,
                source_route="import",
                import_run_id=seeded["import_run_id"],
                command_execution_id=None,
                content_changed=False,
                placement_changed=True,
                completion_changed=False,
                archive_changed=False,
                occurred_at=NOW,
            )
        )
        session.flush()
        session.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == seeded["generation_id"],
                models.DishState.task_id == occurrence.task_id,
                models.DishState.dish_version == 1,
            )
            .values(dish_version=2, placement_version=2, updated_at=NOW)
            .execution_options(synchronize_session=False)
        )
        session.flush()
        session.expire(state)

        with pytest.raises(
            NativeSectionContentMaterializationError,
            match="intervening Dish mutation lineage",
        ):
            materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
            )


def test_rejects_stale_current_content_pointer(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(session, ids)
        occurrence = occurrences[0]
        source = session.get(models.ContentVersion, occurrence.source_content_version_id)
        assert source is not None
        next_version = occurrence.source_dish_version + 1
        competing_content_id = _next(ids)
        competing_body = source.body + "\nCompeting write\n"
        session.add(
            models.DishMutationReceipt(
                generation_id=seeded["generation_id"], task_id=occurrence.task_id,
                dish_version=next_version, source_route="import",
                import_run_id=occurrence.import_run_id, command_execution_id=None,
                content_changed=True, placement_changed=False, completion_changed=False,
                archive_changed=False, occurred_at=NOW,
            )
        )
        session.flush()
        session.add(
            models.ContentVersion(
                content_version_id=competing_content_id, generation_id=seeded["generation_id"],
                task_id=occurrence.task_id, representation_kind="document", title=source.title,
                body=competing_body, identity_scheme=CONTENT_IDENTITY_SCHEME,
                content_identity=content_identity(source.title, competing_body), creator_route="import",
                import_run_id=occurrence.import_run_id, command_execution_id=None,
                predecessor_content_version_id=occurrence.source_content_version_id,
                contract_binding_id=source.contract_binding_id, created_dish_version=next_version,
                created_at=NOW,
            )
        )
        session.flush()
        moved = session.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == seeded["generation_id"],
                models.DishState.task_id == occurrence.task_id,
                models.DishState.dish_version == occurrence.source_dish_version,
            )
            .values(current_content_version_id=competing_content_id, dish_version=next_version, updated_at=NOW)
            .execution_options(synchronize_session=False)
        )
        assert moved.rowcount == 1
        session.flush()

        with pytest.raises(NativeSectionContentMaterializationError, match="current DishState/content pointer moved"):
            materialize_staged_native_section_content(
                session, generation_id=seeded["generation_id"], migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id, materialized_at=NOW,
            )


def test_rejects_conflicting_successor_mutation_slot(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(session, ids)
        occurrence = occurrences[0]
        session.add(
            models.DishMutationReceipt(
                generation_id=seeded["generation_id"], task_id=occurrence.task_id,
                dish_version=occurrence.source_dish_version + 1, source_route="import",
                import_run_id=occurrence.import_run_id, command_execution_id=None,
                content_changed=True, placement_changed=False, completion_changed=False,
                archive_changed=False, occurred_at=NOW,
            )
        )
        session.flush()
        before = int(session.scalar(select(func.count()).select_from(models.ContentVersion)) or 0)
        with pytest.raises(NativeSectionContentMaterializationError, match="successor Dish mutation slot is already occupied"):
            materialize_staged_native_section_content(
                session, generation_id=seeded["generation_id"], migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id, materialized_at=NOW,
            )
        after = int(session.scalar(select(func.count()).select_from(models.ContentVersion)) or 0)
        assert after == before
