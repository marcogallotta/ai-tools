from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg.database import session_scope
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
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from tests.postgresql.test_native_section_content_carry_forward import (
    NOW,
    SOURCE_COMMIT,
    SOURCE_TREE,
    _fixture,
)
from tests.support.postgresql.core import _next

pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


@pytest.fixture(autouse=True)
def _verified_repository_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        "dish_pg.native_section_carry_forward._verified_repository_identity",
        lambda: RepositoryIdentity(commit_sha=SOURCE_COMMIT, tree_sha=SOURCE_TREE),
    )


def _stage_pr3(session: Session, ids: Iterator[uuid.UUID]):
    seeded, expectation, source_rows = _fixture(session, ids)
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
