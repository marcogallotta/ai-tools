from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.native_section_carry_forward import (
    BREAD_SECTION,
    NativeSectionCarryForwardError,
    RepositoryIdentity,
    apply_carry_forward,
    build_carry_forward_plan,
)
from tests.support.postgresql.native_section_content_fixtures import (
    NOW,
    SOURCE_COMMIT,
    SOURCE_TREE,
    _fixture,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


@pytest.fixture(autouse=True)
def _verified_repository_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        "dish_pg.native_section_carry_forward._verified_repository_identity",
        lambda: RepositoryIdentity(commit_sha=SOURCE_COMMIT, tree_sha=SOURCE_TREE),
    )


def test_pr3_stages_native_content_without_switching_current_authority(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, source_rows = _fixture(session, ids)
        section_count_before = int(session.scalar(select(func.count()).select_from(models.Section)) or 0)
        before = {
            task_id: (
                state.current_content_version_id,
                state.dish_version,
                state.section_id,
                state.registry_version_id,
            )
            for task_id, _ in source_rows
            if (state := session.get(models.DishState, (seeded["generation_id"], task_id)))
            is not None
        }
        plan = build_carry_forward_plan(session, expectation=expectation)
        assert len(plan.occurrences) == 23
        assert plan.summary()["ready_baseline_occurrence_count"] == 3
        receipt = apply_carry_forward(
            session,
            expected_snapshot_sha256=plan.source_snapshot_sha256,
            source_commit=SOURCE_COMMIT,
            expectation=expectation,
            now=NOW,
        )

        assert receipt["decision"] == "carry_forward_completed"
        assert receipt["runtime_switched"] is False
        assert receipt["asana_projection"] is False
        assert receipt["current_dish_state_mutated"] is False
        assert receipt["staged_occurrence_count"] == 23
        assert receipt["ready_baseline_occurrence_count"] == 3
        assert receipt["source_commit_sha"] == SOURCE_COMMIT
        assert receipt["source_tree_sha"] == SOURCE_TREE

        active = session.get(models.ActiveSectionCatalog, seeded["generation_id"])
        assert active is not None and active.catalog_revision == 2
        entries = tuple(
            session.scalars(
                select(models.SectionCatalogEntry)
                .where(models.SectionCatalogEntry.catalog_version_id == active.catalog_version_id)
                .order_by(models.SectionCatalogEntry.ordinal)
            )
        )
        assert [entry.display_name for entry in entries[-4:]] == [
            "Vietnamese",
            "Desserts",
            "Hunan",
            "Bread",
        ]
        assert session.get(models.Section, BREAD_SECTION.section_id).logical_name == "Bread"
        assert int(session.scalar(select(func.count()).select_from(models.Section)) or 0) == section_count_before
        assert receipt["preexisting_sections"][-1]["logical_name"] == "Bread"

        occurrences = tuple(
            session.scalars(
                select(models.NativeSectionContentCarryForwardOccurrence).order_by(
                    models.NativeSectionContentCarryForwardOccurrence.task_id
                )
            )
        )
        assert len(occurrences) == 23
        assert all(" — section:" in row.transformed_body for row in occurrences)
        assert all("Destination section:" in row.transformed_body for row in occurrences)
        assert sum(row.verification_baseline_kind == "migration_assigned_ready" for row in occurrences) == 3
        assert (
            session.scalar(select(func.count()).select_from(wf.VerificationSignoff)) == 0
        )

        after = {
            task_id: (
                state.current_content_version_id,
                state.dish_version,
                state.section_id,
                state.registry_version_id,
            )
            for task_id, _ in source_rows
            if (state := session.get(models.DishState, (seeded["generation_id"], task_id)))
            is not None
        }
        assert after == before

        event = session.get(models.AppliedMigrationEvent, uuid.UUID(receipt["migration_event_id"]))
        assert event is not None
        assert event.revision == "0048_native_section_content_carry_forward"
        assert event.details["decision"] == "carry_forward_completed"
        assert event.details["source_commit_sha"] == SOURCE_COMMIT
        assert event.details["source_tree_sha"] == SOURCE_TREE
        assert event.details["ready_baseline_override"]["database_signoffs_fabricated"] is False

        rerun = apply_carry_forward(
            session,
            expected_snapshot_sha256=plan.source_snapshot_sha256,
            source_commit=SOURCE_COMMIT,
            expectation=expectation,
            now=NOW,
        )
        assert rerun["inserted"] is False
        assert rerun["migration_event_id"] == receipt["migration_event_id"]


def test_pr3_rejects_operator_commit_not_matching_executable_checkout(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        _, expectation, _ = _fixture(session, ids)
        plan = build_carry_forward_plan(session, expectation=expectation)
        with pytest.raises(
            NativeSectionCarryForwardError,
            match="does not match the clean checkout",
        ):
            apply_carry_forward(
                session,
                expected_snapshot_sha256=plan.source_snapshot_sha256,
                source_commit="c" * 40,
                expectation=expectation,
                now=NOW,
            )
        assert (
            session.scalar(
                select(func.count()).select_from(
                    models.NativeSectionContentCarryForwardOccurrence
                )
            )
            == 0
        )


def test_pr3_rejects_snapshot_drift_before_any_transition_write(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _ = _fixture(session, ids)
        plan = build_carry_forward_plan(session, expectation=expectation)
        with pytest.raises(
            NativeSectionCarryForwardError,
            match="changed after the approved carry-forward check",
        ):
            apply_carry_forward(
                session,
                expected_snapshot_sha256="f" * 64,
                source_commit=SOURCE_COMMIT,
                expectation=expectation,
                now=NOW,
            )
        assert (
            session.scalar(
                select(func.count()).select_from(
                    models.NativeSectionContentCarryForwardOccurrence
                )
            )
            == 0
        )
        active = session.get(models.ActiveSectionCatalog, seeded["generation_id"])
        assert active is not None and active.catalog_revision == 2
        assert plan.source_snapshot_sha256 != "f" * 64


def test_pr3_carry_forward_rows_are_immutable(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        _, expectation, _ = _fixture(session, ids)
        plan = build_carry_forward_plan(session, expectation=expectation)
        apply_carry_forward(
            session,
            expected_snapshot_sha256=plan.source_snapshot_sha256,
            source_commit=SOURCE_COMMIT,
            expectation=expectation,
            now=NOW,
        )
        row = session.scalar(select(models.NativeSectionContentCarryForwardOccurrence))
        assert row is not None
        row.destination_display_name = "Changed"
        with pytest.raises(IntegrityError):
            session.flush()


def test_pr3_migration_renders_staging_only_without_runtime_root() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    config.attributes["output_buffer"] = buffer
    command.upgrade(config, "0048_native_section_content_carry_forward", sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE native_section_content_carry_forward_occurrences" in rendered
    assert "native_section_content_carry_forward_immutable" in rendered
    assert "CREATE TABLE native_catalog_runtime_attestations" not in rendered
    assert "CREATE TABLE current_native_catalog_runtimes" not in rendered
