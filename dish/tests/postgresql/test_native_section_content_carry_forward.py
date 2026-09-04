from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.native_section_carry_forward import (
    BREAD_SECTION,
    MISSING_DESTINATIONS,
    REQUIRED_SECTIONS,
    CarryForwardExpectation,
    NativeSectionCarryForwardError,
    apply_carry_forward,
    build_carry_forward_plan,
)
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.repositories import CatalogRepository
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from tests.support.postgresql.core import _bootstrap_registry, _next

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
SOURCE_COMMIT = "a" * 40
READY_BASELINE_LINE = "Verified by: Codex - migration-assigned baseline, 2026-08-01"
pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


def _body(*, status: str, destination: str) -> str:
    return (
        "## PROCESS RECORD\n"
        "### Planning brief\n"
        f"Destination section: {destination}\n"
        f"Status: {status}\n"
        + (f"{READY_BASELINE_LINE}\n" if status == "ready" else "Verified by: None\n")
    )


def _add_document(
    session: Session,
    ids: Iterator[uuid.UUID],
    *,
    generation_id: uuid.UUID,
    import_run_id: uuid.UUID,
    binding_id: uuid.UUID,
    registry_version_id: uuid.UUID,
    section_id: uuid.UUID,
    status: str,
    destination: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    task_id = _next(ids)
    content_version_id = _next(ids)
    body = _body(status=status, destination=destination)
    session.add(
        models.DishTask(
            task_id=task_id,
            existence_state="isolated",
            creation_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            created_at=NOW,
            retired_at=None,
        )
    )
    session.flush()
    session.add(
        models.DishMutationReceipt(
            generation_id=generation_id,
            task_id=task_id,
            dish_version=1,
            source_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            content_changed=True,
            placement_changed=True,
            completion_changed=True,
            archive_changed=False,
            occurred_at=NOW,
        )
    )
    session.flush()
    session.add(
        models.ContentVersion(
            content_version_id=content_version_id,
            generation_id=generation_id,
            task_id=task_id,
            representation_kind="document",
            title=f"Dish {task_id}",
            body=body,
            identity_scheme=CONTENT_IDENTITY_SCHEME,
            content_identity=content_identity(f"Dish {task_id}", body),
            creator_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            predecessor_content_version_id=None,
            contract_binding_id=binding_id,
            created_dish_version=1,
            created_at=NOW,
        )
    )
    session.add(
        models.DishState(
            generation_id=generation_id,
            task_id=task_id,
            current_content_version_id=content_version_id,
            section_id=section_id,
            registry_version_id=registry_version_id,
            completed=False,
            completion_reason="imported",
            archived_at=None,
            dish_version=1,
            placement_version=1,
            completion_version=1,
            updated_at=NOW,
        )
    )
    session.flush()
    return task_id, content_version_id


def _fixture(session: Session, ids: Iterator[uuid.UUID]):
    seeded = _bootstrap_registry(
        session,
        ids,
        generation_status="active",
        schema_head=ALEMBIC_HEAD,
    )
    catalog_v1_id = _next(ids)
    catalog_v1_activation_id = _next(ids)
    catalog = CatalogRepository(session)
    catalog.add_section(
        models.Section(
            section_id=seeded["section_id"],
            logical_name="Research Queue",
            lifecycle="active",
            created_at=NOW,
            retired_at=None,
        )
    )
    catalog.install_catalog_revision(
        version=models.SectionCatalogVersion(
            catalog_version_id=catalog_v1_id,
            generation_id=seeded["generation_id"],
            version_number=1,
            contract_binding_id=seeded["binding_id"],
            catalog_sha256="d" * 64,
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW,
        ),
        entries=(
            models.SectionCatalogEntry(
                catalog_version_id=catalog_v1_id,
                section_id=seeded["section_id"],
                ordinal=0,
                display_name="Research Queue",
                workflow_role="research_queue",
            ),
        ),
        activation=models.SectionCatalogActivation(
            catalog_activation_id=catalog_v1_activation_id,
            generation_id=seeded["generation_id"],
            catalog_version_id=catalog_v1_id,
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
    for section in REQUIRED_SECTIONS:
        catalog.add_section(
            models.Section(
                section_id=section.section_id,
                logical_name=section.display_name,
                lifecycle="active",
                created_at=NOW,
                retired_at=None,
            )
        )
    catalog_version_id = _next(ids)
    catalog_activation_id = _next(ids)
    catalog.install_catalog_revision(
        version=models.SectionCatalogVersion(
            catalog_version_id=catalog_version_id,
            generation_id=seeded["generation_id"],
            version_number=2,
            contract_binding_id=seeded["binding_id"],
            catalog_sha256="e" * 64,
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW,
        ),
        entries=(
            models.SectionCatalogEntry(
                catalog_version_id=catalog_version_id,
                section_id=seeded["section_id"],
                ordinal=0,
                display_name="Research Queue",
                workflow_role="research_queue",
            ),
            *(
                models.SectionCatalogEntry(
                    catalog_version_id=catalog_version_id,
                    section_id=section.section_id,
                    ordinal=index,
                    display_name=section.display_name,
                    workflow_role=section.workflow_role,
                )
                for index, section in enumerate(REQUIRED_SECTIONS, start=1)
            ),
        ),
        activation=models.SectionCatalogActivation(
            catalog_activation_id=catalog_activation_id,
            generation_id=seeded["generation_id"],
            catalog_version_id=catalog_version_id,
            activation_route="recovery",
            import_run_id=None,
            command_execution_id=None,
            catalog_revision=2,
            activated_at=NOW,
        ),
        expected_catalog_version_id=catalog_v1_id,
        expected_catalog_activation_id=catalog_v1_activation_id,
        expected_catalog_revision=1,
    )

    rows: list[tuple[uuid.UUID, uuid.UUID]] = []
    # One existing legacy alias proves the ordinary mapping path.
    rows.append(
        _add_document(
            session,
            ids,
            generation_id=seeded["generation_id"],
            import_run_id=seeded["import_run_id"],
            binding_id=seeded["binding_id"],
            registry_version_id=seeded["registry_version_id"],
            section_id=seeded["section_id"],
            status="ready",
            destination="Research Queue — 1217084805070731",
        )
    )
    for missing in MISSING_DESTINATIONS:
        for index in range(missing.expected_documents):
            status = "ready" if missing.display_name == "Vietnamese" and index < 2 else "pending-verification"
            rows.append(
                _add_document(
                    session,
                    ids,
                    generation_id=seeded["generation_id"],
                    import_run_id=seeded["import_run_id"],
                    binding_id=seeded["binding_id"],
                    registry_version_id=seeded["registry_version_id"],
                    section_id=seeded["section_id"],
                    status=status,
                    destination=f"{missing.display_name} — {missing.legacy_gid}",
                )
            )
    expectation = CarryForwardExpectation(
        generation_id=seeded["generation_id"],
        base_catalog_version_id=catalog_version_id,
        base_catalog_activation_id=catalog_activation_id,
        base_catalog_revision=2,
        total_documents=len(rows),
        ready_documents=3,
        pending_verification_documents=20,
        imported_unsigned_ready_documents=3,
        verification_signoffs=0,
        legacy_destination_documents=len(rows),
        ready_legacy_destination_documents=3,
        pending_verification_legacy_destination_documents=20,
        ready_without_legacy_destination=0,
    )
    return seeded, expectation, tuple(rows)


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
    command.upgrade(config, ALEMBIC_HEAD, sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE native_section_content_carry_forward_occurrences" in rendered
    assert "native_section_content_carry_forward_immutable" in rendered
    assert "CREATE TABLE native_catalog_runtime_attestations" not in rendered
    assert "CREATE TABLE current_native_catalog_runtimes" not in rendered
