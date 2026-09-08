"""Shared fixtures for the native-section carry-forward/materializer test suites.

Extracted from tests/postgresql/test_native_section_content_carry_forward.py and
tests/postgresql/test_native_section_content_materializer.py so other test modules
can reuse them without importing another collected test module directly (see
tests/test_test_module_import_contract.py).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg.native_section_carry_forward import (
    MISSING_DESTINATIONS,
    REQUIRED_SECTIONS,
    CarryForwardExpectation,
)
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.repositories import CatalogRepository
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from tests.support.postgresql.core import _bootstrap_registry, _next

NOW = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
SOURCE_COMMIT = "a" * 40
SOURCE_TREE = "b" * 40
READY_BASELINE_LINE = "Verified by: Codex - migration-assigned baseline, 2026-08-01"


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
    identity_scheme: str = CONTENT_IDENTITY_SCHEME,
    content_identity_override: str | None = None,
    title_suffix_after_identity: str = "",
) -> tuple[uuid.UUID, uuid.UUID]:
    task_id = _next(ids)
    content_version_id = _next(ids)
    body = _body(status=status, destination=destination)
    canonical_title = f"Dish {task_id}"
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
            title=canonical_title + title_suffix_after_identity,
            body=body,
            identity_scheme=identity_scheme,
            content_identity=(
                content_identity_override or content_identity(canonical_title, body)
            ),
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


def _fixture(
    session: Session,
    ids: Iterator[uuid.UUID],
    *,
    first_identity_scheme: str = CONTENT_IDENTITY_SCHEME,
    first_content_identity: str | None = None,
    first_title_suffix_after_identity: str = "",
):
    seeded = _bootstrap_registry(
        session,
        ids,
        generation_status="active",
        schema_head=ALEMBIC_HEAD,
    )
    catalog_v1_id = _next(ids)
    catalog_v1_activation_id = _next(ids)
    catalog = CatalogRepository(session)
    if session.get(models.Section, seeded["section_id"]) is None:
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
            identity_scheme=first_identity_scheme,
            content_identity_override=first_content_identity,
            title_suffix_after_identity=first_title_suffix_after_identity,
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
