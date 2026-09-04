"""Bounded PR3 carry-forward from legacy destination GIDs to native Section UUIDs.

This module deliberately stages complete immutable successor document bytes without
moving ``DishState.current_content_version_id``.  Until PR2 establishes the native
runtime root, the live runtime still parses legacy ``name — <Asana GID>`` Planning
syntax.  PR3 therefore prepares exact future occurrences bound to the already-current
native catalog without switching runtime authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dish_tool._task_document_syntax import DESTINATION_RE
from dish_tool.content_versions import content_identity

from . import models
from . import stage3_models as wf
from .recovery_control import migration_revision_sha256
from .repositories import AuthorityRepository, CatalogRepository, CoreAuthorityError

CARRY_FORWARD_REVISION = "0048_native_section_content_carry_forward"
CARRY_FORWARD_PREDECESSOR = "0047_native_section_catalog_foundation"
CARRY_FORWARD_KIND = "native-section-content-carry-forward-v1"
INVENTORY_STORY_GID = "1218192594300807"
MARCO_DECISION_STORY_GID = "1218192690298850"
SECTION_CREATION_STORY_GID = "1218193409368093"
SEMANTIC_OWNER_TASK_GID = "1217878303550695"
RECOVERY_DESIGN_TASK_GID = "1218149197310340"
READY_BASELINE = "Codex - migration-assigned baseline, 2026-08-01"
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NAMESPACE = uuid.UUID("c183d5da-eaa4-4dbf-b727-f214c98cf9c4")


class NativeSectionCarryForwardError(ValueError):
    """The exact reviewed PR3 inventory/identity contract is not satisfied."""


@dataclass(frozen=True)
class MissingDestination:
    legacy_gid: str
    display_name: str
    expected_documents: int
    workflow_role: str
    section_id: uuid.UUID


MISSING_DESTINATIONS = (
    MissingDestination(
        "1217084499118483",
        "Vietnamese",
        15,
        "native-section-4ddc1ad4-3fc0-4a8b-8c55-162664f68b75",
        uuid.UUID("4ddc1ad4-3fc0-4a8b-8c55-162664f68b75"),
    ),
    MissingDestination(
        "1217084805070754",
        "Desserts",
        3,
        "native-section-18e22e03-f27f-4bd6-80f1-71bf84a65a46",
        uuid.UUID("18e22e03-f27f-4bd6-80f1-71bf84a65a46"),
    ),
    MissingDestination(
        "1217084805075175",
        "Hunan",
        4,
        "native-section-c4c736f7-00b9-481e-8600-2d2007472206",
        uuid.UUID("c4c736f7-00b9-481e-8600-2d2007472206"),
    ),
)
BREAD_SECTION = MissingDestination(
    "",
    "Bread",
    0,
    "native-section-4dfbf988-a8d1-48ab-b307-46baf3b47192",
    uuid.UUID("4dfbf988-a8d1-48ab-b307-46baf3b47192"),
)
REQUIRED_SECTIONS = MISSING_DESTINATIONS + (BREAD_SECTION,)


@dataclass(frozen=True)
class CarryForwardExpectation:
    generation_id: uuid.UUID
    base_catalog_version_id: uuid.UUID
    base_catalog_activation_id: uuid.UUID
    base_catalog_revision: int
    total_documents: int
    ready_documents: int
    pending_verification_documents: int
    imported_unsigned_ready_documents: int
    verification_signoffs: int
    legacy_destination_documents: int
    ready_legacy_destination_documents: int
    pending_verification_legacy_destination_documents: int
    ready_without_legacy_destination: int


PRODUCTION_EXPECTATION = CarryForwardExpectation(
    generation_id=uuid.UUID("72d9d4f7-9520-4fd5-b238-e79c8125fca0"),
    base_catalog_version_id=uuid.UUID("b42a348a-7618-4cc3-9204-739bb7aa988c"),
    base_catalog_activation_id=uuid.UUID("da55e228-33c8-4dd5-bfc2-a38bf6324779"),
    base_catalog_revision=2,
    total_documents=387,
    ready_documents=60,
    pending_verification_documents=66,
    imported_unsigned_ready_documents=60,
    verification_signoffs=0,
    legacy_destination_documents=146,
    ready_legacy_destination_documents=59,
    pending_verification_legacy_destination_documents=66,
    ready_without_legacy_destination=1,
)


@dataclass(frozen=True)
class SourceDocument:
    task_id: uuid.UUID
    dish_version: int
    content_version_id: uuid.UUID
    content_identity: str
    creator_route: str
    title: str
    body: str
    status: str | None
    destination_display_name: str | None
    destination_legacy_gid: str | None


@dataclass(frozen=True)
class PlannedOccurrence:
    source: SourceDocument
    target_section_id: uuid.UUID
    transformed_title: str
    transformed_body: str
    transformed_content_identity: str
    verification_baseline_kind: str
    verification_baseline_text: str | None
    transform_sha256: str


@dataclass(frozen=True)
class CarryForwardPlan:
    generation_id: uuid.UUID
    source_snapshot_sha256: str
    counts: dict[str, int]
    occurrences: tuple[PlannedOccurrence, ...]
    required_sections: tuple[MissingDestination, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "decision": "carry_forward_required",
            "generation_id": str(self.generation_id),
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "counts": dict(self.counts),
            "staged_occurrence_count": len(self.occurrences),
            "ready_baseline_occurrence_count": sum(
                item.verification_baseline_kind == "migration_assigned_ready"
                for item in self.occurrences
            ),
            "preexisting_sections": [
                {
                    "logical_name": item.display_name,
                    "section_id": str(item.section_id),
                    "legacy_gid": item.legacy_gid or None,
                    "workflow_role": item.workflow_role,
                }
                for item in self.required_sections
            ],
            "runtime_switched": False,
            "asana_projection": False,
        }


def _sha_json(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deterministic_id(generation_id: uuid.UUID, snapshot_sha: str, kind: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{generation_id}:{snapshot_sha}:{kind}")


def _single_field(body: str, label: str) -> str | None:
    prefix = f"{label}:"
    values = [line[len(prefix) :].strip() for line in body.splitlines() if line.startswith(prefix)]
    if len(values) > 1:
        raise NativeSectionCarryForwardError(f"document contains duplicate {label} fields")
    return values[0] if values else None


def _legacy_destination(body: str) -> tuple[str, str] | None:
    value = _single_field(body, "Destination section")
    if value is None or not value.strip():
        return None
    match = DESTINATION_RE.fullmatch(value)
    if match is not None:
        return match.group("name"), match.group("gid")
    if " — section:" in value:
        raise NativeSectionCarryForwardError(
            "native destination syntax already exists before the PR2 runtime switch"
        )
    raise NativeSectionCarryForwardError(
        f"current Destination section is neither legacy nor native syntax: {value!r}"
    )


def _replace_destination(body: str, *, display_name: str, section_id: uuid.UUID) -> str:
    lines = body.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.startswith("Destination section:")]
    if len(matches) != 1:
        raise NativeSectionCarryForwardError(
            "carry-forward document must contain exactly one Destination section field"
        )
    index = matches[0]
    ending = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"Destination section: {display_name} — section:{section_id}{ending}"
    return "".join(lines)


def _current_documents(
    session: Session, generation_id: uuid.UUID, *, lock: bool
) -> tuple[SourceDocument, ...]:
    stmt = (
        select(models.DishState, models.ContentVersion)
        .join(
            models.ContentVersion,
            (
                (models.ContentVersion.generation_id == models.DishState.generation_id)
                & (models.ContentVersion.task_id == models.DishState.task_id)
                & (
                    models.ContentVersion.content_version_id
                    == models.DishState.current_content_version_id
                )
            ),
        )
        .where(models.DishState.generation_id == generation_id)
        .order_by(models.DishState.task_id)
    )
    if lock and session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(of=models.DishState)
    rows: list[SourceDocument] = []
    for state, version in session.execute(stmt):
        destination = _legacy_destination(version.body)
        rows.append(
            SourceDocument(
                task_id=state.task_id,
                dish_version=state.dish_version,
                content_version_id=version.content_version_id,
                content_identity=version.content_identity,
                creator_route=version.creator_route,
                title=version.title,
                body=version.body,
                status=_single_field(version.body, "Status"),
                destination_display_name=None if destination is None else destination[0],
                destination_legacy_gid=None if destination is None else destination[1],
            )
        )
    return tuple(rows)


def _validate_counts(
    *,
    expectation: CarryForwardExpectation,
    documents: tuple[SourceDocument, ...],
    signoff_count: int,
) -> dict[str, int]:
    ready = tuple(row for row in documents if row.status == "ready")
    pending = tuple(row for row in documents if row.status == "pending-verification")
    legacy = tuple(row for row in documents if row.destination_legacy_gid is not None)
    ready_legacy = tuple(row for row in legacy if row.status == "ready")
    pending_legacy = tuple(row for row in legacy if row.status == "pending-verification")
    imported_ready = tuple(row for row in ready if row.creator_route == "import")
    ready_without_destination = tuple(
        row for row in ready if row.destination_legacy_gid is None
    )
    counts = {
        "total_documents": len(documents),
        "ready_documents": len(ready),
        "pending_verification_documents": len(pending),
        "imported_unsigned_ready_documents": len(imported_ready),
        "verification_signoffs": signoff_count,
        "legacy_destination_documents": len(legacy),
        "ready_legacy_destination_documents": len(ready_legacy),
        "pending_verification_legacy_destination_documents": len(pending_legacy),
        "ready_without_legacy_destination": len(ready_without_destination),
    }
    expected = {
        "total_documents": expectation.total_documents,
        "ready_documents": expectation.ready_documents,
        "pending_verification_documents": expectation.pending_verification_documents,
        "imported_unsigned_ready_documents": expectation.imported_unsigned_ready_documents,
        "verification_signoffs": expectation.verification_signoffs,
        "legacy_destination_documents": expectation.legacy_destination_documents,
        "ready_legacy_destination_documents": expectation.ready_legacy_destination_documents,
        "pending_verification_legacy_destination_documents": expectation.pending_verification_legacy_destination_documents,
        "ready_without_legacy_destination": expectation.ready_without_legacy_destination,
    }
    if counts != expected:
        raise NativeSectionCarryForwardError(
            f"approved production inventory changed: expected {expected}, got {counts}"
        )
    if any(_single_field(row.body, "Verified by") != READY_BASELINE for row in ready):
        raise NativeSectionCarryForwardError(
            "Marco's ready-content override applies only to the exact migration-assigned baseline"
        )
    return counts


def build_carry_forward_plan(
    session: Session,
    *,
    expectation: CarryForwardExpectation = PRODUCTION_EXPECTATION,
    lock: bool = False,
) -> CarryForwardPlan:
    generations_stmt = select(models.AuthorityGeneration).where(
        models.AuthorityGeneration.status == "active"
    )
    if lock and session.get_bind().dialect.name == "postgresql":
        generations_stmt = generations_stmt.with_for_update()
    generations = tuple(session.scalars(generations_stmt))
    if len(generations) != 1 or generations[0].generation_id != expectation.generation_id:
        raise NativeSectionCarryForwardError(
            "carry-forward requires the exact approved active production generation"
        )
    generation = generations[0]

    active_stmt = select(models.ActiveSectionCatalog).where(
        models.ActiveSectionCatalog.generation_id == generation.generation_id
    )
    if lock and session.get_bind().dialect.name == "postgresql":
        active_stmt = active_stmt.with_for_update()
    active = session.scalar(active_stmt)
    if active is None:
        raise NativeSectionCarryForwardError("PR1 native catalog foundation is not active")
    if (
        active.catalog_version_id != expectation.base_catalog_version_id
        or active.catalog_activation_id != expectation.base_catalog_activation_id
        or active.catalog_revision != expectation.base_catalog_revision
    ):
        raise NativeSectionCarryForwardError("approved PR1 catalog identity has moved")
    try:
        catalog = CatalogRepository(session).active_catalog_contract(generation.generation_id)
    except CoreAuthorityError as exc:
        raise NativeSectionCarryForwardError(str(exc)) from exc

    entry_by_id = {entry.section_id: entry for entry in catalog.entries}
    for section in REQUIRED_SECTIONS:
        by_id = session.get(models.Section, section.section_id)
        if (
            by_id is None
            or by_id.logical_name != section.display_name
            or by_id.lifecycle != "active"
        ):
            raise NativeSectionCarryForwardError(
                f"required pre-existing native Section identity is missing or changed: {section.display_name}"
            )
        entry = entry_by_id.get(section.section_id)
        if (
            entry is None
            or entry.display_name != section.display_name
            or entry.workflow_role != section.workflow_role
        ):
            raise NativeSectionCarryForwardError(
                f"required pre-existing native catalog entry is missing or changed: {section.display_name}"
            )

    documents = _current_documents(session, generation.generation_id, lock=lock)
    signoff_count = int(
        session.scalar(select(func.count()).select_from(wf.VerificationSignoff)) or 0
    )
    counts = _validate_counts(
        expectation=expectation, documents=documents, signoff_count=signoff_count
    )

    active_entry_ids = {entry.section_id for entry in catalog.entries}
    explicit_by_gid = {item.legacy_gid: item for item in MISSING_DESTINATIONS}
    missing_counts = {item.legacy_gid: 0 for item in MISSING_DESTINATIONS}
    planned: list[PlannedOccurrence] = []

    for row in documents:
        gid = row.destination_legacy_gid
        if gid is None:
            continue
        display_name = str(row.destination_display_name)
        alias_rows = tuple(
            session.scalars(
                select(models.SectionExternalAlias).where(
                    models.SectionExternalAlias.external_system == "asana",
                    models.SectionExternalAlias.external_id == gid,
                    models.SectionExternalAlias.state == "active",
                )
            )
        )
        explicit = explicit_by_gid.get(gid)
        if explicit is not None:
            if display_name != explicit.display_name:
                raise NativeSectionCarryForwardError(
                    f"legacy destination {gid} label changed from approved {explicit.display_name!r}"
                )
            if any(alias.section_id != explicit.section_id for alias in alias_rows):
                raise NativeSectionCarryForwardError(
                    f"legacy destination {gid} conflicts with the approved native Section identity"
                )
            target_section_id = explicit.section_id
            missing_counts[gid] += 1
        elif alias_rows:
            if len(alias_rows) != 1:
                raise NativeSectionCarryForwardError(
                    f"legacy destination {gid} has conflicting native mapping evidence"
                )
            target_section_id = alias_rows[0].section_id
            if target_section_id not in active_entry_ids:
                raise NativeSectionCarryForwardError(
                    f"legacy destination {gid} resolves outside the active native catalog"
                )
        else:
            raise NativeSectionCarryForwardError(
                f"legacy destination {gid} has no approved native Section mapping"
            )

        baseline_kind = "none"
        baseline_text = None
        if row.status == "ready":
            if row.creator_route != "import":
                raise NativeSectionCarryForwardError(
                    "Marco's no-reverification override is limited to imported ready content"
                )
            baseline_text = _single_field(row.body, "Verified by")
            if baseline_text != READY_BASELINE:
                raise NativeSectionCarryForwardError(
                    "ready content no longer has the approved migration-assigned baseline"
                )
            baseline_kind = "migration_assigned_ready"

        transformed_body = _replace_destination(
            row.body, display_name=display_name, section_id=target_section_id
        )
        transformed_identity = content_identity(row.title, transformed_body)
        transform_sha = _sha_json(
            {
                "format": CARRY_FORWARD_KIND,
                "generation_id": str(generation.generation_id),
                "task_id": str(row.task_id),
                "source_content_version_id": str(row.content_version_id),
                "source_content_identity": row.content_identity,
                "source_dish_version": row.dish_version,
                "target_section_id": str(target_section_id),
                "destination_display_name": display_name,
                "destination_legacy_gid": gid,
                "transformed_content_identity": transformed_identity,
                "verification_baseline_kind": baseline_kind,
                "verification_baseline_text": baseline_text,
            }
        )
        planned.append(
            PlannedOccurrence(
                source=row,
                target_section_id=target_section_id,
                transformed_title=row.title,
                transformed_body=transformed_body,
                transformed_content_identity=transformed_identity,
                verification_baseline_kind=baseline_kind,
                verification_baseline_text=baseline_text,
                transform_sha256=transform_sha,
            )
        )

    expected_missing = {
        item.legacy_gid: item.expected_documents for item in MISSING_DESTINATIONS
    }
    if missing_counts != expected_missing:
        raise NativeSectionCarryForwardError(
            f"approved missing-destination inventory changed: expected {expected_missing}, got {missing_counts}"
        )

    snapshot = {
        "format": CARRY_FORWARD_KIND,
        "generation_id": str(generation.generation_id),
        "base_catalog_version_id": str(catalog.catalog_version.catalog_version_id),
        "base_catalog_activation_id": str(catalog.catalog_activation.catalog_activation_id),
        "base_catalog_revision": catalog.active_catalog.catalog_revision,
        "base_catalog_sha256": catalog.catalog_version.catalog_sha256,
        "counts": counts,
        "required_preexisting_sections": [
            {
                "logical_name": item.display_name,
                "section_id": str(item.section_id),
                "legacy_gid": item.legacy_gid or None,
                "expected_documents": item.expected_documents,
                "workflow_role": item.workflow_role,
            }
            for item in REQUIRED_SECTIONS
        ],
        "documents": [
            {
                "task_id": str(row.task_id),
                "dish_version": row.dish_version,
                "content_version_id": str(row.content_version_id),
                "content_identity": row.content_identity,
                "creator_route": row.creator_route,
                "title": row.title,
                "body": row.body,
                "status": row.status,
                "destination_display_name": row.destination_display_name,
                "destination_legacy_gid": row.destination_legacy_gid,
            }
            for row in documents
        ],
    }
    return CarryForwardPlan(
        generation_id=generation.generation_id,
        source_snapshot_sha256=_sha_json(snapshot),
        counts=counts,
        occurrences=tuple(planned),
        required_sections=REQUIRED_SECTIONS,
    )


def _helper_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _existing_receipt(
    session: Session,
    *,
    generation_id: uuid.UUID,
    expected_snapshot_sha256: str,
    source_commit: str,
) -> dict[str, Any] | None:
    event = session.scalar(
        select(models.AppliedMigrationEvent).where(
            models.AppliedMigrationEvent.generation_id == generation_id,
            models.AppliedMigrationEvent.revision == CARRY_FORWARD_REVISION,
            models.AppliedMigrationEvent.outcome == "applied",
        )
    )
    if event is None:
        return None
    if event.details.get("source_snapshot_sha256") != expected_snapshot_sha256:
        raise NativeSectionCarryForwardError(
            "existing PR3 migration event belongs to a different source snapshot"
        )
    if event.details.get("source_commit_sha") != source_commit:
        raise NativeSectionCarryForwardError(
            "existing PR3 migration event belongs to a different executable commit"
        )
    count = int(
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
    expected_count = int(event.details.get("staged_occurrence_count", -1))
    if count != expected_count:
        raise NativeSectionCarryForwardError(
            "existing PR3 migration event/occurrence readback is inconsistent"
        )
    return dict(event.details) | {
        "migration_event_id": str(event.migration_event_id),
        "inserted": False,
    }


def apply_carry_forward(
    session: Session,
    *,
    expected_snapshot_sha256: str,
    source_commit: str,
    expectation: CarryForwardExpectation = PRODUCTION_EXPECTATION,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot_sha256):
        raise NativeSectionCarryForwardError(
            "expected snapshot SHA-256 must be 64 lowercase hexadecimal characters"
        )
    if not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise NativeSectionCarryForwardError(
            "source commit must be the exact 40-character lowercase Git SHA"
        )
    now = now or datetime.now(timezone.utc)

    existing = _existing_receipt(
        session,
        generation_id=expectation.generation_id,
        expected_snapshot_sha256=expected_snapshot_sha256,
        source_commit=source_commit,
    )
    if existing is not None:
        return existing

    plan = build_carry_forward_plan(session, expectation=expectation, lock=True)
    if plan.source_snapshot_sha256 != expected_snapshot_sha256:
        raise NativeSectionCarryForwardError(
            "current production content changed after the approved carry-forward check"
        )

    generation = session.get(models.AuthorityGeneration, plan.generation_id)
    if generation is None:
        raise NativeSectionCarryForwardError("active generation disappeared")
    current = CatalogRepository(session).active_catalog_contract(plan.generation_id)

    import_run_id = _deterministic_id(
        plan.generation_id, plan.source_snapshot_sha256, "import-run"
    )
    migration_event_id = _deterministic_id(
        plan.generation_id, plan.source_snapshot_sha256, "migration-event"
    )
    session.add(
        models.ImportRun(
            import_run_id=import_run_id,
            source_commit=source_commit,
            source_release=CARRY_FORWARD_KIND,
            legacy_generation_id=f"native-section-carry-forward:{plan.generation_id}",
            baseline_high_water_mark=plan.source_snapshot_sha256,
            source_bundle_sha256=plan.source_snapshot_sha256,
            status="complete",
            started_at=now,
            completed_at=now,
            provenance={
                "import_kind": CARRY_FORWARD_KIND,
                "source_task_gid": SEMANTIC_OWNER_TASK_GID,
                "recovery_design_task_gid": RECOVERY_DESIGN_TASK_GID,
                "inventory_story_gid": INVENTORY_STORY_GID,
                "marco_decision_story_gid": MARCO_DECISION_STORY_GID,
                "section_creation_story_gid": SECTION_CREATION_STORY_GID,
                "ready_baseline_override": True,
                "sections_preexisting": True,
                "asana_projection": False,
            },
        )
    )
    session.flush()

    details: dict[str, Any] = {
        "authority_transition": "native_section_content_carry_forward_v1",
        "decision": "carry_forward_completed",
        "generation_id": str(plan.generation_id),
        "source_task_gid": SEMANTIC_OWNER_TASK_GID,
        "recovery_design_task_gid": RECOVERY_DESIGN_TASK_GID,
        "inventory_story_gid": INVENTORY_STORY_GID,
        "marco_decision_story_gid": MARCO_DECISION_STORY_GID,
        "section_creation_story_gid": SECTION_CREATION_STORY_GID,
        "sections_preexisting": True,
        "repository": "marcogallotta/ai-tools",
        "source_commit_sha": source_commit,
        "source_snapshot_sha256": plan.source_snapshot_sha256,
        "import_run_id": str(import_run_id),
        "source_catalog_version_id": str(current.catalog_version.catalog_version_id),
        "source_catalog_activation_id": str(current.catalog_activation.catalog_activation_id),
        "source_catalog_revision": current.active_catalog.catalog_revision,
        "target_catalog_version_id": str(current.catalog_version.catalog_version_id),
        "target_catalog_activation_id": str(current.catalog_activation.catalog_activation_id),
        "target_catalog_revision": current.active_catalog.catalog_revision,
        "honest_contract_binding_id": str(current.honest_binding.binding_id),
        "inventory_counts": dict(plan.counts),
        "staged_occurrence_count": len(plan.occurrences),
        "ready_baseline_occurrence_count": sum(
            item.verification_baseline_kind == "migration_assigned_ready"
            for item in plan.occurrences
        ),
        "ready_without_destination_unchanged": plan.counts[
            "ready_without_legacy_destination"
        ],
        "ready_baseline_override": {
            "waived_rule": "unsigned/imported ready becomes pending-verification",
            "baseline_text": READY_BASELINE,
            "database_signoffs_fabricated": False,
        },
        "preexisting_sections": [
            {
                "logical_name": item.display_name,
                "section_id": str(item.section_id),
                "legacy_gid": item.legacy_gid or None,
                "workflow_role": item.workflow_role,
            }
            for item in REQUIRED_SECTIONS
        ],
        "asana_projection": False,
        "runtime_switched": False,
        "current_dish_state_mutated": False,
        "helper_code_sha256": _helper_sha256(),
    }
    AuthorityRepository(session).add_migration_event(
        models.AppliedMigrationEvent(
            migration_event_id=migration_event_id,
            generation_id=plan.generation_id,
            revision=CARRY_FORWARD_REVISION,
            predecessor_revision=CARRY_FORWARD_PREDECESSOR,
            migration_code_sha256=migration_revision_sha256(CARRY_FORWARD_REVISION),
            dish_release=generation.dish_release,
            initiator="dish-pg-native-section-carry-forward",
            outcome="applied",
            started_at=now,
            terminal_at=now,
            details=details,
        )
    )

    for occurrence in plan.occurrences:
        source = occurrence.source
        session.add(
            models.NativeSectionContentCarryForwardOccurrence(
                carry_forward_id=_deterministic_id(
                    plan.generation_id,
                    plan.source_snapshot_sha256,
                    f"occurrence:{source.task_id}:{source.content_version_id}",
                ),
                generation_id=plan.generation_id,
                task_id=source.task_id,
                source_content_version_id=source.content_version_id,
                source_dish_version=source.dish_version,
                source_content_identity=source.content_identity,
                source_status=source.status,
                target_catalog_version_id=current.catalog_version.catalog_version_id,
                target_section_id=occurrence.target_section_id,
                destination_legacy_gid=str(source.destination_legacy_gid),
                destination_display_name=str(source.destination_display_name),
                transformed_title=occurrence.transformed_title,
                transformed_body=occurrence.transformed_body,
                transformed_content_identity=occurrence.transformed_content_identity,
                verification_baseline_kind=occurrence.verification_baseline_kind,
                verification_baseline_text=occurrence.verification_baseline_text,
                transform_sha256=occurrence.transform_sha256,
                import_run_id=import_run_id,
                migration_event_id=migration_event_id,
                recorded_at=now,
            )
        )
    session.flush()

    for source in (item.source for item in plan.occurrences):
        state = session.get(models.DishState, (plan.generation_id, source.task_id))
        if (
            state is None
            or state.dish_version != source.dish_version
            or state.current_content_version_id != source.content_version_id
        ):
            raise NativeSectionCarryForwardError(
                "current Dish authority changed during PR3 carry-forward"
            )

    return details | {
        "migration_event_id": str(migration_event_id),
        "inserted": True,
    }
