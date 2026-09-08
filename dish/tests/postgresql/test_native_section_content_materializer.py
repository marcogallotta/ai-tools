from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.native_catalog_runtime_finalizer import (
    FINALIZER_REVISION,
    NativeCatalogRuntimeFinalizerError,
    finalize_native_catalog_runtime_authority,
)
from dish_pg.native_history_placement_repair import (
    SNAPSHOT_RECORD_COUNT,
    _load_snapshot,
)
from dish_pg.native_section_carry_forward import (
    RepositoryIdentity,
    apply_carry_forward,
    build_carry_forward_plan,
)
from dish_pg.native_section_content_materializer import (
    NativeSectionContentMaterializationError,
    PostStagingContentCorrection,
    materialize_staged_native_section_content,
    materialized_content_version_id,
)
from dish_pg.repositories import CatalogRepository
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from tests.support.postgresql.core import _next
from tests.support.postgresql.native_section_content_fixtures import (
    NOW,
    SOURCE_COMMIT,
    SOURCE_TREE,
    _fixture,
)
from tests.support.postgresql.native_section_content_materializer_fixtures import (
    _complete_after_staging,
    _stage_pr3,
)

pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _verified_repository_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        "dish_pg.native_section_carry_forward._verified_repository_identity",
        lambda: RepositoryIdentity(commit_sha=SOURCE_COMMIT, tree_sha=SOURCE_TREE),
    )


def _install_successor_catalog(
    session: Session,
    ids: Iterator[uuid.UUID],
    *,
    seeded,
    expectation,
    rename_target: uuid.UUID | None = None,
    omit_target: uuid.UUID | None = None,
) -> uuid.UUID:
    current = session.get(models.ActiveSectionCatalog, seeded["generation_id"])
    base = session.get(
        models.SectionCatalogVersion, expectation.base_catalog_version_id
    )
    assert current is not None and base is not None
    version_id, activation_id, added_section_id = _next(ids), _next(ids), _next(ids)
    session.add(
        models.Section(
            section_id=added_section_id,
            logical_name=f"additive-{added_section_id}",
            lifecycle="active",
            created_at=NOW,
            retired_at=None,
        )
    )
    session.flush()
    entries = []
    for entry in session.scalars(
        select(models.SectionCatalogEntry)
        .where(
            models.SectionCatalogEntry.catalog_version_id
            == expectation.base_catalog_version_id
        )
        .order_by(models.SectionCatalogEntry.ordinal)
    ):
        if entry.section_id == omit_target:
            continue
        entries.append(
            models.SectionCatalogEntry(
                catalog_version_id=version_id,
                section_id=entry.section_id,
                ordinal=len(entries),
                display_name=(
                    f"{entry.display_name} renamed"
                    if entry.section_id == rename_target
                    else entry.display_name
                ),
                workflow_role=entry.workflow_role,
            )
        )
    entries.append(
        models.SectionCatalogEntry(
            catalog_version_id=version_id,
            section_id=added_section_id,
            ordinal=len(entries),
            display_name="Additive Section",
            workflow_role="additive_role",
        )
    )
    CatalogRepository(session).install_catalog_revision(
        version=models.SectionCatalogVersion(
            catalog_version_id=version_id,
            generation_id=seeded["generation_id"],
            version_number=base.version_number + 1,
            contract_binding_id=base.contract_binding_id,
            catalog_sha256="e" * 64,
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW + timedelta(minutes=2),
        ),
        entries=entries,
        activation=models.SectionCatalogActivation(
            catalog_activation_id=activation_id,
            generation_id=seeded["generation_id"],
            catalog_version_id=version_id,
            activation_route="recovery",
            import_run_id=None,
            command_execution_id=None,
            catalog_revision=current.catalog_revision + 1,
            activated_at=NOW + timedelta(minutes=2),
        ),
        expected_catalog_version_id=current.catalog_version_id,
        expected_catalog_activation_id=current.catalog_activation_id,
        expected_catalog_revision=current.catalog_revision,
    )
    return version_id


def test_materializes_staged_successors_inside_caller_transaction(core_db) -> None:
    factory, ids = core_db
    with factory() as session:
        with session.begin():
            seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(
                session, ids
            )
            before = {
                row.task_id: (
                    state.section_id,
                    state.registry_version_id,
                    state.completion_version,
                )
                for row in occurrences
                if (
                    state := session.get(
                        models.DishState, (seeded["generation_id"], row.task_id)
                    )
                )
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
            assert (
                result.occurrence_count,
                result.materialized_count,
                result.already_materialized_count,
            ) == (23, 23, 0)

            for occurrence in occurrences:
                state = session.get(
                    models.DishState, (seeded["generation_id"], occurrence.task_id)
                )
                successor_id = materialized_content_version_id(
                    occurrence.carry_forward_id
                )
                successor = session.get(models.ContentVersion, successor_id)
                assert state is not None and successor is not None
                assert (
                    successor.predecessor_content_version_id
                    == occurrence.source_content_version_id
                )
                assert successor.body == occurrence.transformed_body
                assert (
                    successor.content_identity
                    == occurrence.transformed_content_identity
                )
                assert (
                    successor.created_dish_version == occurrence.source_dish_version + 1
                )
                assert state.current_content_version_id == successor_id
                assert state.catalog_version_id == expectation.base_catalog_version_id
                assert (
                    state.dish_version
                    == state.placement_version
                    == occurrence.source_dish_version + 1
                )
                assert (
                    state.section_id,
                    state.registry_version_id,
                    state.completion_version,
                ) == before[occurrence.task_id]
                mutation = session.get(
                    models.DishMutationReceipt,
                    (
                        seeded["generation_id"],
                        occurrence.task_id,
                        occurrence.source_dish_version + 1,
                    ),
                )
                assert mutation is not None
                assert (mutation.content_changed, mutation.placement_changed) == (
                    True,
                    True,
                )
                assert (mutation.completion_changed, mutation.archive_changed) == (
                    False,
                    False,
                )

            retry = materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
            )
            assert (retry.materialized_count, retry.already_materialized_count) == (
                0,
                23,
            )


def test_materializes_when_current_content_predates_source_dish_version(
    core_db,
) -> None:
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


def test_finalizer_roots_compatible_additive_successor_catalog(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, _event_id, occurrences = _stage_pr3(session, ids)
        successor_id = _install_successor_catalog(
            session,
            ids,
            seeded=seeded,
            expectation=expectation,
        )
        result = finalize_native_catalog_runtime_authority(
            session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
        )
        assert result.catalog_version_id == successor_id
        assert result.materialization.materialized_count == len(occurrences)
        assert all(
            session.get(
                models.DishState,
                (seeded["generation_id"], occurrence.task_id),
            ).catalog_version_id
            == successor_id
            for occurrence in occurrences
        )


def test_history_repair_snapshot_preserves_distinct_operator_categories() -> None:
    sections, records, _digest = _load_snapshot()

    assert len(records) == SNAPSHOT_RECORD_COUNT == 265
    assert sections["1217202747684673"] == "Indo/Malay/Singapore"
    assert sections["1215259129474856"] == "Southeast Asia Misc."
    assert sections["1215259129474861"] == "Mediterranean herbs"
    assert sections["1215259129474860"] == "Mediterranean"
    assert len({task_gid for task_gid, _section_gid in records}) == 265


def test_materializer_accepts_only_an_exact_row_bound_legacy_label_correction(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, event_id, occurrences = _stage_pr3(session, ids)
        occurrence = occurrences[0]
        successor_id = _install_successor_catalog(
            session,
            ids,
            seeded=seeded,
            expectation=expectation,
            rename_target=occurrence.target_section_id,
        )
        current_entry = session.get(
            models.SectionCatalogEntry,
            (successor_id, occurrence.target_section_id),
        )
        assert current_entry is not None

        result = materialize_staged_native_section_content(
            session,
            generation_id=seeded["generation_id"],
            migration_event_id=event_id,
            catalog_version_id=successor_id,
            materialized_at=NOW,
            destination_label_corrections={
                occurrence.carry_forward_id: (
                    occurrence.target_section_id,
                    occurrence.destination_display_name,
                    current_entry.display_name,
                )
            },
        )

        assert result.materialized_count == len(occurrences)


def test_migrations_repair_historical_null_then_enforce_native_not_null(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'native-placement.sqlite3'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0050_native_catalog_runtime_authority_switch")

    engine = create_engine(database_url, future=True)
    factory = sessionmaker(bind=engine, class_=Session, future=True)
    ids = (uuid.UUID(int=value) for value in range(1, 1000))
    with session_scope(factory) as session:
        seeded, _expectation, _, _event_id, occurrences = _stage_pr3(session, ids)
        historical = occurrences[0]
        historical_task_id = historical.task_id
        historical_target_section_id = historical.target_section_id
        connection = session.connection()
        update_trigger = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='dish_states_validate_update'"
        ).scalar_one()
        connection.exec_driver_sql("DROP TRIGGER dish_states_validate_update")
        session.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == seeded["generation_id"],
                models.DishState.task_id == historical.task_id,
            )
            .values(section_id=None)
            .execution_options(synchronize_session=False)
        )
        connection.exec_driver_sql(update_trigger)
    engine.dispose()

    from dish_pg import migrate

    evidence = {"phases": [], "recorded_at": None}
    journal = type("Journal", (), {"write": lambda self, payload: None})()
    migrate._run_native_placement_sequence(
        type(
            "Args",
            (),
            {
                "database_url": database_url,
                "expected_database_name": str(make_url(database_url).database),
            },
        )(),
        evidence,
        journal,
        "f" * 40,
    )

    engine = create_engine(database_url, future=True)
    try:
        with Session(engine) as session:
            repaired = session.get(
                models.DishState, (seeded["generation_id"], historical_task_id)
            )
            assert repaired is not None
            assert repaired.section_id == historical_target_section_id
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("dish_states")
        }
        assert columns["section_id"]["nullable"] is False
        assert any(
            foreign_key["constrained_columns"] == ["section_id"]
            and foreign_key["referred_table"] == "sections"
            for foreign_key in inspect(engine).get_foreign_keys("dish_states")
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize("change", ("rename", "remove"))
def test_finalizer_rejects_incompatible_staged_destination_successor(
    core_db, change
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, _event_id, occurrences = _stage_pr3(session, ids)
        target = occurrences[0].target_section_id
        _install_successor_catalog(
            session,
            ids,
            seeded=seeded,
            expectation=expectation,
            rename_target=target if change == "rename" else None,
            omit_target=target if change == "remove" else None,
        )
        with pytest.raises(
            NativeCatalogRuntimeFinalizerError,
            match="destination is not the exact target catalog entry",
        ):
            finalize_native_catalog_runtime_authority(
                session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
            )


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
    with (
        pytest.raises(
            NativeCatalogRuntimeFinalizerError,
            match="injected failure after rebased staged materialization",
        ),
        session_scope(factory) as session,
    ):
        finalize_native_catalog_runtime_authority(
            session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
        )

    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(models.AppliedMigrationEvent)
                .where(
                    models.AppliedMigrationEvent.generation_id
                    == seeded["generation_id"],
                    models.AppliedMigrationEvent.revision == FINALIZER_REVISION,
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(models.NativeCatalogRuntimeAttestation)
                .where(
                    models.NativeCatalogRuntimeAttestation.generation_id
                    == seeded["generation_id"]
                )
            )
            == 0
        )
        assert (
            session.get(models.CurrentNativeCatalogRuntime, seeded["generation_id"])
            is None
        )
        assert session.get(models.ContentVersion, successor_id) is None
        assert (
            session.get(
                models.DishMutationReceipt,
                (seeded["generation_id"], rebased_task_id, 3),
            )
            is None
        )
        for occurrence in occurrences:
            state = session.get(
                models.DishState, (seeded["generation_id"], occurrence.task_id)
            )
            assert state is not None
            assert (
                state.current_content_version_id
                == source_content_ids[occurrence.task_id]
            )
            assert state.catalog_version_id is None
        state = session.get(
            models.DishState, (seeded["generation_id"], rebased_task_id)
        )
        assert state is not None
        assert state.dish_version == state.completion_version == 2
        assert state.completed is True


def test_rejects_intervening_placement_receipt_before_materialization(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(
            session, ids
        )
        occurrence = occurrences[0]
        state = session.get(
            models.DishState, (seeded["generation_id"], occurrence.task_id)
        )
        assert (
            state is not None
            and occurrence.source_dish_version == state.dish_version == 1
        )
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
        seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(
            session, ids
        )
        occurrence = occurrences[0]
        source = session.get(
            models.ContentVersion, occurrence.source_content_version_id
        )
        assert source is not None
        next_version = occurrence.source_dish_version + 1
        competing_content_id = _next(ids)
        competing_body = source.body + "\nCompeting write\n"
        session.add(
            models.DishMutationReceipt(
                generation_id=seeded["generation_id"],
                task_id=occurrence.task_id,
                dish_version=next_version,
                source_route="import",
                import_run_id=occurrence.import_run_id,
                command_execution_id=None,
                content_changed=True,
                placement_changed=False,
                completion_changed=False,
                archive_changed=False,
                occurred_at=NOW,
            )
        )
        session.flush()
        session.add(
            models.ContentVersion(
                content_version_id=competing_content_id,
                generation_id=seeded["generation_id"],
                task_id=occurrence.task_id,
                representation_kind="document",
                title=source.title,
                body=competing_body,
                identity_scheme=CONTENT_IDENTITY_SCHEME,
                content_identity=content_identity(source.title, competing_body),
                creator_route="import",
                import_run_id=occurrence.import_run_id,
                command_execution_id=None,
                predecessor_content_version_id=occurrence.source_content_version_id,
                contract_binding_id=source.contract_binding_id,
                created_dish_version=next_version,
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
            .values(
                current_content_version_id=competing_content_id,
                dish_version=next_version,
                updated_at=NOW,
            )
            .execution_options(synchronize_session=False)
        )
        assert moved.rowcount == 1
        session.flush()

        with pytest.raises(
            NativeSectionContentMaterializationError,
            match="current DishState/content pointer moved",
        ):
            materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
            )


def test_rejects_unknown_post_staging_content_correction(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, migration_event_id, _ = _stage_pr3(session, ids)
        unknown = _next(ids)
        correction = PostStagingContentCorrection(
            task_id=_next(ids),
            source_content_version_id=_next(ids),
            current_content_version_id=_next(ids),
            command_execution_id=_next(ids),
            current_content_identity="0" * 64,
            current_contract_binding_id=_next(ids),
            current_section_id=_next(ids),
            legacy_destination_line="Destination section: unknown",
            destination_display_name="unknown",
        )

        with pytest.raises(
            NativeSectionContentMaterializationError,
            match="correction set contains an unknown occurrence",
        ):
            materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
                post_staging_content_corrections={unknown: correction},
            )


def test_rejects_conflicting_successor_mutation_slot(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, migration_event_id, occurrences = _stage_pr3(
            session, ids
        )
        occurrence = occurrences[0]
        session.add(
            models.DishMutationReceipt(
                generation_id=seeded["generation_id"],
                task_id=occurrence.task_id,
                dish_version=occurrence.source_dish_version + 1,
                source_route="import",
                import_run_id=occurrence.import_run_id,
                command_execution_id=None,
                content_changed=True,
                placement_changed=False,
                completion_changed=False,
                archive_changed=False,
                occurred_at=NOW,
            )
        )
        session.flush()
        before = int(
            session.scalar(select(func.count()).select_from(models.ContentVersion)) or 0
        )
        with pytest.raises(
            NativeSectionContentMaterializationError,
            match="successor Dish mutation slot is already occupied",
        ):
            materialize_staged_native_section_content(
                session,
                generation_id=seeded["generation_id"],
                migration_event_id=migration_event_id,
                catalog_version_id=expectation.base_catalog_version_id,
                materialized_at=NOW,
            )
        after = int(
            session.scalar(select(func.count()).select_from(models.ContentVersion)) or 0
        )
        assert after == before
