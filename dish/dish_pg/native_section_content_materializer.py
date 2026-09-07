"""Materialize PR3-staged native Section content inside a caller-owned transaction.

The 0048 carry-forward already decided and persisted the successor document bytes and
native destination identities.  This module does not transform content, choose a
Section, establish native runtime authority, or commit.  It only turns those immutable
staged occurrences into the exact current-content/catalog-placement mutations that the
PR2f finalizer may include in its larger authority-switch transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
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
class PostStagingContentCorrection:
    """Exact audited command mutation that the 0048 transform must rebase onto."""

    task_id: uuid.UUID
    source_content_version_id: uuid.UUID
    current_content_version_id: uuid.UUID
    command_execution_id: uuid.UUID
    current_content_identity: str
    current_contract_binding_id: uuid.UUID
    current_section_id: uuid.UUID
    legacy_destination_line: str
    destination_display_name: str


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
) -> tuple[models.AppliedMigrationEvent, int, uuid.UUID]:
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
    staged_catalog_value = details.get("target_catalog_version_id")
    try:
        staged_catalog_version_id = uuid.UUID(str(staged_catalog_value))
    except (TypeError, ValueError):
        staged_catalog_version_id = None
    if (
        details.get("decision") != "carry_forward_completed"
        or details.get("generation_id") != str(generation_id)
        or staged_catalog_version_id is None
    ):
        raise NativeSectionContentMaterializationError(
            "0048 AppliedMigrationEvent details do not match the requested materialization"
        )
    expected_count = details.get("staged_occurrence_count")
    if not isinstance(expected_count, int) or expected_count <= 0:
        raise NativeSectionContentMaterializationError(
            "0048 AppliedMigrationEvent has no valid staged occurrence count"
        )
    return event, expected_count, staged_catalog_version_id


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
        or content_identity(source.title, source.body)
        != occurrence.source_content_identity
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
    successor_id: uuid.UUID,
    dish_version: int,
    title: str,
    body: str,
    content_identity_value: str,
    predecessor_content_version_id: uuid.UUID,
    contract_binding_id: uuid.UUID,
) -> bool:
    return bool(
        content is not None
        and content.content_version_id == successor_id
        and content.generation_id == occurrence.generation_id
        and content.task_id == occurrence.task_id
        and content.representation_kind == "document"
        and content.title == title
        and content.body == body
        and content.identity_scheme == CONTENT_IDENTITY_SCHEME
        and content.content_identity == content_identity_value
        and content.creator_route == "import"
        and content.import_run_id == occurrence.import_run_id
        and content.command_execution_id is None
        and content.predecessor_content_version_id == predecessor_content_version_id
        and content.contract_binding_id == contract_binding_id
        and content.created_dish_version == dish_version
    )


def _rewrite_exact_destination(
    body: str,
    *,
    legacy_line: str,
    display_name: str,
    section_id: uuid.UUID,
) -> str:
    lines = body.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n").startswith("Destination section:")
    ]
    if len(matches) != 1 or lines[matches[0]].rstrip("\r\n") != legacy_line:
        raise NativeSectionContentMaterializationError(
            "post-staging content has unexpected Destination section syntax"
        )
    index = matches[0]
    ending = lines[index][len(lines[index].rstrip("\r\n")) :]
    lines[index] = (
        f"Destination section: {display_name} — section:{section_id}{ending}"
    )
    return "".join(lines)


def _validated_post_staging_source(
    session: Session,
    *,
    occurrence: models.NativeSectionContentCarryForwardOccurrence,
    source: models.ContentVersion,
    correction: PostStagingContentCorrection,
    target_display_name: str,
) -> tuple[models.ContentVersion, str, str, str]:
    current = session.get(models.ContentVersion, correction.current_content_version_id)
    receipt = session.get(
        models.DishMutationReceipt,
        (
            occurrence.generation_id,
            occurrence.task_id,
            occurrence.source_dish_version + 1,
        ),
    )
    if (
        correction.task_id != occurrence.task_id
        or correction.source_content_version_id
        != occurrence.source_content_version_id
        or correction.destination_display_name != target_display_name
        or current is None
        or current.content_version_id != correction.current_content_version_id
        or current.generation_id != occurrence.generation_id
        or current.task_id != occurrence.task_id
        or current.representation_kind != "document"
        or current.identity_scheme != CONTENT_IDENTITY_SCHEME
        or current.content_identity != correction.current_content_identity
        or content_identity(current.title, current.body)
        != correction.current_content_identity
        or current.creator_route != "command_execution"
        or current.import_run_id is not None
        or current.command_execution_id != correction.command_execution_id
        or current.predecessor_content_version_id
        != occurrence.source_content_version_id
        or current.contract_binding_id != correction.current_contract_binding_id
        or current.created_dish_version != occurrence.source_dish_version + 1
        or receipt is None
        or receipt.source_route != "command_execution"
        or receipt.import_run_id is not None
        or receipt.command_execution_id != correction.command_execution_id
        or not receipt.content_changed
        or not receipt.placement_changed
        or receipt.completion_changed
        or receipt.archive_changed
    ):
        raise NativeSectionContentMaterializationError(
            "post-staging content correction does not match exact audited lineage"
        )
    body = _rewrite_exact_destination(
        current.body,
        legacy_line=correction.legacy_destination_line,
        display_name=correction.destination_display_name,
        section_id=correction.current_section_id,
    )
    return current, current.title, body, content_identity(current.title, body)


def materialize_staged_native_section_content(
    session: Session,
    *,
    generation_id: uuid.UUID,
    migration_event_id: uuid.UUID,
    catalog_version_id: uuid.UUID,
    materialized_at: datetime,
    destination_label_corrections: Mapping[uuid.UUID, tuple[uuid.UUID, str, str]]
    | None = None,
    post_staging_content_corrections: Mapping[
        uuid.UUID, PostStagingContentCorrection
    ]
    | None = None,
) -> NativeSectionContentMaterializationResult:
    """Apply the staged 0048 successors without owning or committing the transaction.

    The caller must provide the exact 0048 event and catalog identity that its enclosing
    authority transition already selected.  Exact retries are no-ops.  Any movement of
    the source/current pointer, occupied successor mutation slot, or mismatched staged
    content fails before this function writes a new successor row.
    """

    _event, expected_count, staged_catalog_version_id = _event_contract(
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

    corrections = dict(destination_label_corrections or {})
    used_corrections: set[uuid.UUID] = set()
    occurrence_ids = {row.carry_forward_id for row in occurrences}
    post_staging_corrections = dict(post_staging_content_corrections or {})
    unknown_post_staging_corrections = set(post_staging_corrections) - occurrence_ids
    if unknown_post_staging_corrections:
        raise NativeSectionContentMaterializationError(
            "post-staging content correction set contains an unknown occurrence"
        )
    used_post_staging_corrections: set[uuid.UUID] = set()
    pending: list[
        tuple[
            models.NativeSectionContentCarryForwardOccurrence,
            models.ContentVersion,
            models.DishState,
            uuid.UUID,
            int,
            str,
            str,
            str,
        ]
    ] = []
    already = 0

    # Validate the full batch before introducing any new successor artifacts.
    for occurrence in occurrences:
        if occurrence.target_catalog_version_id != staged_catalog_version_id:
            raise NativeSectionContentMaterializationError(
                "staged carry-forward occurrence targets a different historical catalog version"
            )
        target_entry = session.get(
            models.SectionCatalogEntry,
            (catalog_version_id, occurrence.target_section_id),
        )
        if target_entry is None:
            raise NativeSectionContentMaterializationError(
                "staged carry-forward destination is not the exact target catalog entry"
            )
        if target_entry.display_name != occurrence.destination_display_name:
            correction = corrections.get(occurrence.carry_forward_id)
            if correction != (
                occurrence.target_section_id,
                occurrence.destination_display_name,
                target_entry.display_name,
            ):
                raise NativeSectionContentMaterializationError(
                    "staged carry-forward destination is not the exact target catalog entry"
                )
            used_corrections.add(occurrence.carry_forward_id)
        source = _validate_source(
            session.get(
                models.ContentVersion,
                (occurrence.source_content_version_id),
            ),
            occurrence,
        )
        state = states[occurrence.task_id]
        repaired_section_id = (
            occurrence.target_section_id
            if state.section_id is None
            else state.section_id
        )
        if repaired_section_id not in target_entries:
            raise NativeSectionContentMaterializationError(
                "current DishState placement is not representable in the current target catalog"
            )

        successor_id = materialized_content_version_id(occurrence.carry_forward_id)
        successor = session.get(models.ContentVersion, successor_id)
        post_staging = post_staging_corrections.get(occurrence.carry_forward_id)
        materialization_source = source
        materialized_title = occurrence.transformed_title
        materialized_body = occurrence.transformed_body
        materialized_identity = occurrence.transformed_content_identity
        if post_staging is not None:
            current_target_entry = session.get(
                models.SectionCatalogEntry,
                (catalog_version_id, post_staging.current_section_id),
            )
            if current_target_entry is None:
                raise NativeSectionContentMaterializationError(
                    "post-staging placement is not in the current target catalog"
                )
            (
                materialization_source,
                materialized_title,
                materialized_body,
                materialized_identity,
            ) = _validated_post_staging_source(
                session,
                occurrence=occurrence,
                source=source,
                correction=post_staging,
                target_display_name=current_target_entry.display_name,
            )
            used_post_staging_corrections.add(occurrence.carry_forward_id)

        if state.current_content_version_id == successor_id:
            if successor is None:
                raise NativeSectionContentMaterializationError(
                    "materialized current content is missing"
                )
            materialized_version = successor.created_dish_version
            materialized_receipt = session.get(
                models.DishMutationReceipt,
                (generation_id, occurrence.task_id, materialized_version),
            )
            later_receipts = tuple(
                session.scalars(
                    select(models.DishMutationReceipt)
                    .where(
                        models.DishMutationReceipt.generation_id == generation_id,
                        models.DishMutationReceipt.task_id == occurrence.task_id,
                        models.DishMutationReceipt.dish_version > materialized_version,
                        models.DishMutationReceipt.dish_version <= state.dish_version,
                    )
                    .order_by(models.DishMutationReceipt.dish_version)
                )
            )
            if (
                tuple(row.dish_version for row in later_receipts)
                != tuple(range(materialized_version + 1, state.dish_version + 1))
                or any(row.content_changed for row in later_receipts)
                or state.catalog_version_id != catalog_version_id
                or state.section_id not in target_entries
                or not _receipt_matches(materialized_receipt, occurrence)
                or not _content_matches(
                    successor,
                    occurrence=occurrence,
                    successor_id=successor_id,
                    dish_version=materialized_version,
                    title=materialized_title,
                    body=materialized_body,
                    content_identity_value=materialized_identity,
                    predecessor_content_version_id=(
                        materialization_source.content_version_id
                    ),
                    contract_binding_id=materialization_source.contract_binding_id,
                )
            ):
                raise NativeSectionContentMaterializationError(
                    "existing staged-content materialization conflicts with expected authority state"
                )
            already += 1
            continue

        expected_current_content_id = materialization_source.content_version_id
        if state.current_content_version_id != expected_current_content_id:
            raise NativeSectionContentMaterializationError(
                "current DishState/content pointer moved since 0048 staging"
            )
        if post_staging is not None:
            if (
                state.dish_version != materialization_source.created_dish_version
                or state.section_id != post_staging.current_section_id
            ):
                raise NativeSectionContentMaterializationError(
                    "post-staging DishState does not match exact audited placement"
                )
        else:
            if state.dish_version < occurrence.source_dish_version:
                raise NativeSectionContentMaterializationError(
                    "current DishState/content pointer moved since 0048 staging"
                )
            intervening = tuple(
                session.scalars(
                    select(models.DishMutationReceipt)
                    .where(
                        models.DishMutationReceipt.generation_id == generation_id,
                        models.DishMutationReceipt.task_id == occurrence.task_id,
                        models.DishMutationReceipt.dish_version
                        > occurrence.source_dish_version,
                        models.DishMutationReceipt.dish_version <= state.dish_version,
                    )
                    .order_by(models.DishMutationReceipt.dish_version)
                )
            )
            if tuple(row.dish_version for row in intervening) != tuple(
                range(occurrence.source_dish_version + 1, state.dish_version + 1)
            ) or any(
                row.content_changed or row.placement_changed for row in intervening
            ):
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
        pending.append(
            (
                occurrence,
                materialization_source,
                state,
                successor_id,
                next_version,
                materialized_title,
                materialized_body,
                materialized_identity,
            )
        )

    if used_corrections != set(corrections):
        raise NativeSectionContentMaterializationError(
            "staged carry-forward label correction set is not an exact occurrence match"
        )
    if used_post_staging_corrections != set(post_staging_corrections):
        raise NativeSectionContentMaterializationError(
            "post-staging content correction set is not an exact occurrence match"
        )

    for (
        occurrence,
        source,
        state,
        successor_id,
        next_version,
        materialized_title,
        materialized_body,
        materialized_identity,
    ) in pending:
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
                title=materialized_title,
                body=materialized_body,
                identity_scheme=CONTENT_IDENTITY_SCHEME,
                content_identity=materialized_identity,
                creator_route="import",
                import_run_id=occurrence.import_run_id,
                command_execution_id=None,
                predecessor_content_version_id=source.content_version_id,
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
                == source.content_version_id,
                models.DishState.catalog_version_id.is_(None),
            )
            .values(
                current_content_version_id=successor_id,
                section_id=(
                    occurrence.target_section_id
                    if state.section_id is None
                    else state.section_id
                ),
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
