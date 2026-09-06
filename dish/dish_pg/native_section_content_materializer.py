"""Materialize PR3-staged native Section content inside a caller-owned transaction.

The 0048 carry-forward already decided and persisted the successor document bytes and
native destination identities.  This module does not transform content, choose a
Section, establish native runtime authority, or commit.  It only turns those immutable
staged occurrences into the exact current-content/catalog-placement mutations that the
PR2f finalizer may include in its larger authority-switch transaction.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity

from . import models
from .native_section_carry_forward import CARRY_FORWARD_REVISION

_MATERIALIZED_CONTENT_NAME = "native-section-staged-content-materialization-v1"
_HISTORICAL_IMPORTED_IDENTITY_SCHEME = "2"


class NativeSectionContentMaterializationError(ValueError):
    """The staged 0048 content cannot be materialized from the current authority state."""


@dataclass(frozen=True)
class NativeSectionContentMaterializationResult:
    generation_id: uuid.UUID
    migration_event_id: uuid.UUID
    catalog_version_id: uuid.UUID
    occurrence_count: int
    materialized_count: int
    already_materialized_count: int


def materialized_content_version_id(carry_forward_id: uuid.UUID) -> uuid.UUID:
    """Return the deterministic successor ContentVersion identity for one staged row."""

    return uuid.uuid5(carry_forward_id, _MATERIALIZED_CONTENT_NAME)


def _event_contract(
    session: Session,
    *,
    generation_id: uuid.UUID,
    migration_event_id: uuid.UUID,
    catalog_version_id: uuid.UUID,
) -> tuple[models.AppliedMigrationEvent, int]:
    event = session.get(models.AppliedMigrationEvent, migration_event_id)
    if event is None:
        raise NativeSectionContentMaterializationError(
            "required 0048 AppliedMigrationEvent is missing"
        )
    if (
        event.generation_id != generation_id
        or event.revision != CARRY_FORWARD_REVISION
        or event.outcome != "applied"
    ):
        raise NativeSectionContentMaterializationError(
            "AppliedMigrationEvent is not the exact same-generation applied 0048 event"
        )
    details = event.details if isinstance(event.details, dict) else {}
    if (
        details.get("decision") != "carry_forward_completed"
        or details.get("generation_id") != str(generation_id)
        or details.get("target_catalog_version_id") != str(catalog_version_id)
    ):
        raise NativeSectionContentMaterializationError(
            "0048 AppliedMigrationEvent details do not match the requested materialization"
        )
    expected_count = details.get("staged_occurrence_count")
    if not isinstance(expected_count, int) or expected_count <= 0:
        raise NativeSectionContentMaterializationError(
            "0048 AppliedMigrationEvent has no valid staged occurrence count"
        )
    return event, expected_count


def _validate_source(
    source: models.ContentVersion | None,
    occurrence: models.NativeSectionContentCarryForwardOccurrence,
) -> models.ContentVersion:
    if source is None:
        raise NativeSectionContentMaterializationError(
            "staged carry-forward source ContentVersion is missing"
        )
    historical_import_identity = (
        source.identity_scheme == _HISTORICAL_IMPORTED_IDENTITY_SCHEME
        and source.representation_kind == "document"
        and source.creator_route == "import"
        and source.import_run_id is not None
        and source.command_execution_id is None
    )
    if (
        source.generation_id != occurrence.generation_id
        or source.task_id != occurrence.task_id
        or source.content_version_id != occurrence.source_content_version_id
        or (
            source.identity_scheme != CONTENT_IDENTITY_SCHEME
            and not historical_import_identity
        )
        or source.content_identity != occurrence.source_content_identity
        or content_identity(source.title, source.body) != occurrence.source_content_identity
    ):
        raise NativeSectionContentMaterializationError(
            "staged carry-forward source occurrence no longer matches immutable source content"
        )
    if (
        content_identity(occurrence.transformed_title, occurrence.transformed_body)
        != occurrence.transformed_content_identity
    ):
        raise NativeSectionContentMaterializationError(
            "staged carry-forward successor bytes do not match their persisted content identity"
        )
    return source


def _receipt_matches(
    receipt: models.DishMutationReceipt | None,
    occurrence: models.NativeSectionContentCarryForwardOccurrence,
) -> bool:
    return bool(
        receipt is not None
        and receipt.source_route == "import"
        and receipt.import_run_id == occurrence.import_run_id
        and receipt.command_execution_id is None
        and receipt.content_changed
        and receipt.placement_changed
        and not receipt.completion_changed
        and not receipt.archive_changed
    )


def _content_matches(
    content: models.ContentVersion | None,
    *,
    occurrence: models.NativeSectionContentCarryForwardOccurrence,
    source: models.ContentVersion,
    successor_id: uuid.UUID,
    dish_version: int,
) -> bool:
    return bool(
        content is not None
        and content.content_version_id == successor_id
        and content.generation_id == occurrence.generation_id
        and content.task_id == occurrence.task_id
        and content.representation_kind == "document"
        and content.title == occurrence.transformed_title
        and content.body == occurrence.transformed_body
        and content.identity_scheme == CONTENT_IDENTITY_SCHEME
        and content.content_identity == occurrence.transformed_content_identity
        and content.creator_route == "import"
        and content.import_run_id == occurrence.import_run_id
        and content.command_execution_id is None
        and content.predecessor_content_version_id
        == occurrence.source_content_version_id
        and content.contract_binding_id == source.contract_binding_id
        and content.created_dish_version == dish_version
    )


def materialize_staged_native_section_content(
    session: Session,
    *,
    generation_id: uuid.UUID,
    migration_event_id: uuid.UUID,
    catalog_version_id: uuid.UUID,
    materialized_at: datetime,
) -> NativeSectionContentMaterializationResult:
    """Apply the staged 0048 successors without owning or committing the transaction.

    The caller must provide the exact 0048 event and catalog identity that its enclosing
    authority transition already selected.  Exact retries are no-ops.  Any movement of
    the source/current pointer, occupied successor mutation slot, or mismatched staged
    content fails before this function writes a new successor row.
    """

    _event, expected_count = _event_contract(
        session,
        generation_id=generation_id,
        migration_event_id=migration_event_id,
        catalog_version_id=catalog_version_id,
    )
    catalog = session.get(models.SectionCatalogVersion, catalog_version_id)
    active = session.get(models.ActiveSectionCatalog, generation_id)
    if (
        catalog is None
        or catalog.generation_id != generation_id
        or active is None
        or active.catalog_version_id != catalog_version_id
    ):
        raise NativeSectionContentMaterializationError(
            "requested 0048 target catalog is not the exact active same-generation catalog"
        )

    occurrences = tuple(
        session.scalars(
            select(models.NativeSectionContentCarryForwardOccurrence)
            .where(
                models.NativeSectionContentCarryForwardOccurrence.generation_id
                == generation_id,
                models.NativeSectionContentCarryForwardOccurrence.migration_event_id
                == migration_event_id,
            )
            .order_by(models.NativeSectionContentCarryForwardOccurrence.task_id)
        )
    )
    generation_count = int(
        session.scalar(
            select(func.count())
            .select_from(models.NativeSectionContentCarryForwardOccurrence)
            .where(
                models.NativeSectionContentCarryForwardOccurrence.generation_id
                == generation_id
            )
        )
        or 0
    )
    if len(occurrences) != expected_count or generation_count != expected_count:
        raise NativeSectionContentMaterializationError(
            "0048 event and staged occurrence set are not an exact generation-bound match"
        )

    task_ids = [occurrence.task_id for occurrence in occurrences]
    state_stmt = (
        select(models.DishState)
        .where(
            models.DishState.generation_id == generation_id,
            models.DishState.task_id.in_(task_ids),
        )
        .order_by(models.DishState.task_id)
    )
    if session.get_bind().dialect.name == "postgresql":
        state_stmt = state_stmt.with_for_update()
    states = {state.task_id: state for state in session.scalars(state_stmt)}
    if len(states) != expected_count:
        raise NativeSectionContentMaterializationError(
            "one or more staged carry-forward tasks have no current DishState"
        )

    target_entries = {
        entry.section_id
        for entry in session.scalars(
            select(models.SectionCatalogEntry).where(
                models.SectionCatalogEntry.catalog_version_id == catalog_version_id
            )
        )
    }

    pending: list[
        tuple[
            models.NativeSectionContentCarryForwardOccurrence,
            models.ContentVersion,
            models.DishState,
            uuid.UUID,
        ]
    ] = []
    already = 0

    # Validate the full batch before introducing any new successor artifacts.
    for occurrence in occurrences:
        if occurrence.target_catalog_version_id != catalog_version_id:
            raise NativeSectionContentMaterializationError(
                "staged carry-forward occurrence targets a different catalog version"
            )
        target_entry = session.get(
            models.SectionCatalogEntry,
            (catalog_version_id, occurrence.target_section_id),
        )
        if (
            target_entry is None
            or target_entry.display_name != occurrence.destination_display_name
        ):
            raise NativeSectionContentMaterializationError(
                "staged carry-forward destination is not the exact target catalog entry"
            )
        source = _validate_source(
            session.get(
                models.ContentVersion,
                (
                    occurrence.source_content_version_id
                ),
            ),
            occurrence,
        )
        state = states[occurrence.task_id]
        if state.section_id is None or state.section_id not in target_entries:
            raise NativeSectionContentMaterializationError(
                "current DishState placement is not representable in the staged target catalog"
            )

        successor_id = materialized_content_version_id(occurrence.carry_forward_id)
        successor = session.get(models.ContentVersion, successor_id)

        if state.current_content_version_id == successor_id:
            materialized_version = state.dish_version
            materialized_receipt = session.get(
                models.DishMutationReceipt,
                (generation_id, occurrence.task_id, materialized_version),
            )
            if (
                state.placement_version != materialized_version
                or state.catalog_version_id != catalog_version_id
                or state.completion_version >= materialized_version
                or not _receipt_matches(materialized_receipt, occurrence)
                or not _content_matches(
                    successor,
                    occurrence=occurrence,
                    source=source,
                    successor_id=successor_id,
                    dish_version=materialized_version,
                )
            ):
                raise NativeSectionContentMaterializationError(
                    "existing staged-content materialization conflicts with expected authority state"
                )
            already += 1
            continue

        if (
            state.current_content_version_id != occurrence.source_content_version_id
            or state.dish_version < occurrence.source_dish_version
        ):
            raise NativeSectionContentMaterializationError(
                "current DishState/content pointer moved since 0048 staging"
            )
        intervening = tuple(
            session.scalars(
                select(models.DishMutationReceipt)
                .where(
                    models.DishMutationReceipt.generation_id == generation_id,
                    models.DishMutationReceipt.task_id == occurrence.task_id,
                    models.DishMutationReceipt.dish_version > occurrence.source_dish_version,
                    models.DishMutationReceipt.dish_version <= state.dish_version,
                )
                .order_by(models.DishMutationReceipt.dish_version)
            )
        )
        if tuple(row.dish_version for row in intervening) != tuple(
            range(occurrence.source_dish_version + 1, state.dish_version + 1)
        ) or any(row.content_changed or row.placement_changed for row in intervening):
            raise NativeSectionContentMaterializationError(
                "intervening Dish mutation lineage is incomplete or changed content/placement"
            )
        if state.catalog_version_id is not None:
            raise NativeSectionContentMaterializationError(
                "source DishState already carries an unexpected native catalog placement"
            )
        next_version = state.dish_version + 1
        next_receipt = session.get(
            models.DishMutationReceipt,
            (generation_id, occurrence.task_id, next_version),
        )
        if successor is not None or next_receipt is not None:
            raise NativeSectionContentMaterializationError(
                "successor Dish mutation slot is already occupied by conflicting materialization"
            )
        pending.append((occurrence, source, state, successor_id, next_version))

    for occurrence, source, state, successor_id, next_version in pending:
        session.add(
            models.DishMutationReceipt(
                generation_id=generation_id,
                task_id=occurrence.task_id,
                dish_version=next_version,
                source_route="import",
                import_run_id=occurrence.import_run_id,
                command_execution_id=None,
                content_changed=True,
                placement_changed=True,
                completion_changed=False,
                archive_changed=False,
                occurred_at=materialized_at,
            )
        )
        session.flush()
        session.add(
            models.ContentVersion(
                content_version_id=successor_id,
                generation_id=generation_id,
                task_id=occurrence.task_id,
                representation_kind="document",
                title=occurrence.transformed_title,
                body=occurrence.transformed_body,
                identity_scheme=CONTENT_IDENTITY_SCHEME,
                content_identity=occurrence.transformed_content_identity,
                creator_route="import",
                import_run_id=occurrence.import_run_id,
                command_execution_id=None,
                predecessor_content_version_id=occurrence.source_content_version_id,
                contract_binding_id=source.contract_binding_id,
                created_dish_version=next_version,
                created_at=materialized_at,
            )
        )
        session.flush()
        result = session.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == generation_id,
                models.DishState.task_id == occurrence.task_id,
                models.DishState.dish_version == next_version - 1,
                models.DishState.current_content_version_id
                == occurrence.source_content_version_id,
                models.DishState.catalog_version_id.is_(None),
            )
            .values(
                current_content_version_id=successor_id,
                catalog_version_id=catalog_version_id,
                dish_version=next_version,
                placement_version=next_version,
                updated_at=materialized_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise NativeSectionContentMaterializationError(
                "DishState materialization CAS lost to a concurrent writer"
            )
        session.flush()
        session.expire(state)

    return NativeSectionContentMaterializationResult(
        generation_id=generation_id,
        migration_event_id=migration_event_id,
        catalog_version_id=catalog_version_id,
        occurrence_count=expected_count,
        materialized_count=len(pending),
        already_materialized_count=already,
    )
