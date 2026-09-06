from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.native_catalog_runtime_finalizer import (
    FINALIZER_REVISION,
    NativeCatalogRuntimeFinalizerError,
    finalize_native_catalog_runtime_authority,
)
from dish_pg.native_section_carry_forward import RepositoryIdentity
from dish_pg.native_section_content_materializer import materialized_content_version_id
from tests.postgresql.test_native_section_content_carry_forward import (
    NOW,
    SOURCE_COMMIT,
    SOURCE_TREE,
)
from tests.postgresql.test_native_section_content_materializer import (
    _complete_after_staging,
    _stage_pr3,
)

pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


@pytest.fixture(autouse=True)
def _verified_repository_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        "dish_pg.native_section_carry_forward._verified_repository_identity",
        lambda: RepositoryIdentity(commit_sha=SOURCE_COMMIT, tree_sha=SOURCE_TREE),
    )


def test_finalizer_rejects_missing_intervening_receipt(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, _expectation, _, _event_id, occurrences = _stage_pr3(session, ids)
        occurrence = occurrences[0]
        task_id = occurrence.task_id
        source_content_id = occurrence.source_content_version_id
        successor_id = materialized_content_version_id(occurrence.carry_forward_id)
        assert occurrence.source_dish_version == 1

    with session_scope(factory) as session:
        assert _complete_after_staging(session, ids, seeded, task_id) == 2
        missing = session.get(
            models.DishMutationReceipt,
            (seeded["generation_id"], task_id, 2),
        )
        assert missing is not None
        session.delete(missing)

    with pytest.raises(
        NativeCatalogRuntimeFinalizerError,
        match="intervening Dish mutation lineage is incomplete",
    ):
        with session_scope(factory) as session:
            finalize_native_catalog_runtime_authority(
                session,
                source_commit_sha="f" * 40,
                now=NOW + timedelta(hours=1),
            )

    with session_scope(factory) as session:
        state = session.get(models.DishState, (seeded["generation_id"], task_id))
        assert state is not None
        assert state.dish_version == state.completion_version == 2
        assert state.completed is True
        assert state.current_content_version_id == source_content_id
        assert state.catalog_version_id is None
        assert session.get(models.ContentVersion, successor_id) is None
        assert session.get(
            models.DishMutationReceipt,
            (seeded["generation_id"], task_id, 3),
        ) is None
        assert session.scalar(
            select(func.count())
            .select_from(models.AppliedMigrationEvent)
            .where(
                models.AppliedMigrationEvent.generation_id == seeded["generation_id"],
                models.AppliedMigrationEvent.revision == FINALIZER_REVISION,
            )
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(models.NativeCatalogRuntimeAttestation)
            .where(
                models.NativeCatalogRuntimeAttestation.generation_id
                == seeded["generation_id"]
            )
        ) == 0
        assert session.get(
            models.CurrentNativeCatalogRuntime,
            seeded["generation_id"],
        ) is None
