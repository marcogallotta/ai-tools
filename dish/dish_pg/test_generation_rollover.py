"""TEST-only authority-generation rollover for fixture-contamination recovery.

This is deliberately not a general generation-management surface. It is fenced to the
maintained ``dish_stage_a_test`` database and only accepts operator-supplied exact Stage 6
identities whose persisted provenance matches the known first-admission fixture contamination incident.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from . import models
from . import reservation_models as reservations
from . import stage5_models as tx
from . import stage6_models as rel
from .bootstrap import require_git_head
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .release import ALEMBIC_HEAD
from .repositories import RegistryRepository
from .transition import ProjectionService, ShadowService

TEST_DATABASE_NAME = "dish_stage_a_test"
ROLLOVER_REASON = "test-fixture contamination recovery"
CREATION_REASON = "test_fixture_recovery"
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SPECIAL_ROLES = {"research_queue", "verification_queue"}
_FIXTURE_SHA256 = "a" * 64
_FIXTURE_SOURCE_COMMIT = "42619b9"
_FIXTURE_SOURCE_RELEASE = "dish-42619b9"
_FIXTURE_TIMESTAMP = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)
_FIXTURE_TASK_ID = uuid.UUID(int=10)
_FIXTURE_REHEARSAL_ENVIRONMENT = f"production-shaped@{_FIXTURE_SHA256}"


class GenerationRolloverError(ValueError):
    """The requested TEST generation rollover is unsafe or does not match the incident."""


@dataclass(frozen=True)
class ContaminationEvidence:
    candidate_id: uuid.UUID
    cutover_run_id: uuid.UUID
    reservation_id: uuid.UUID
    shadow_baseline_id: uuid.UUID
    projection_epoch_id: uuid.UUID


@dataclass(frozen=True)
class GenerationRolloverResult:
    predecessor_generation_id: uuid.UUID
    generation_id: uuid.UUID
    import_run_id: uuid.UUID
    registry_version_id: uuid.UUID
    registry_activation_id: uuid.UUID
    shadow_baseline_id: uuid.UUID
    projection_epoch_id: uuid.UUID
    contamination: ContaminationEvidence
    source_commit: str
    snapshot_sha256: str
    task_count: int
    rolled_over_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "reason": ROLLOVER_REASON,
            "predecessor_generation_id": str(self.predecessor_generation_id),
            "generation_id": str(self.generation_id),
            "import_run_id": str(self.import_run_id),
            "registry_version_id": str(self.registry_version_id),
            "registry_activation_id": str(self.registry_activation_id),
            "shadow_baseline_id": str(self.shadow_baseline_id),
            "projection_epoch_id": str(self.projection_epoch_id),
            "contaminated_candidate_id": str(self.contamination.candidate_id),
            "contaminated_cutover_run_id": str(self.contamination.cutover_run_id),
            "contaminated_reservation_id": str(self.contamination.reservation_id),
            "source_commit": self.source_commit,
            "snapshot_sha256": self.snapshot_sha256,
            "task_count": self.task_count,
            "rolled_over_at": self.rolled_over_at.isoformat(),
        }


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GenerationRolloverError("rollover clock must return a timezone-aware datetime")


def require_test_database_url(database_url: str) -> None:
    """Fail closed before opening any connection unless the URL names exact TEST."""
    if not database_url.strip():
        raise GenerationRolloverError("DISH_PG_DATABASE_URL must be set explicitly")
    try:
        url = make_url(database_url)
    except Exception as exc:  # noqa: BLE001 - normalized to one safety error
        raise GenerationRolloverError("DISH_PG_DATABASE_URL is not a valid SQLAlchemy URL") from exc
    if url.get_backend_name() != "postgresql":
        raise GenerationRolloverError("generation rollover requires PostgreSQL")
    database = url.database or ""
    if "prod" in database.lower():
        raise GenerationRolloverError("generation rollover refuses database names containing 'prod'")
    if database != TEST_DATABASE_NAME:
        raise GenerationRolloverError(
            f"generation rollover requires exact TEST database {TEST_DATABASE_NAME!r}, got {database!r}"
        )


def _connected_database_name(session: Session) -> str:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        raise GenerationRolloverError("generation rollover requires a PostgreSQL session")
    return str(session.scalar(text("SELECT current_database()")) or "")


def _require_test_database_name(database_name: str) -> None:
    if "prod" in database_name.lower():
        raise GenerationRolloverError("connected database name contains 'prod'")
    if database_name != TEST_DATABASE_NAME:
        raise GenerationRolloverError(
            f"connected database must be exact TEST database {TEST_DATABASE_NAME!r}, got {database_name!r}"
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _contamination_evidence(
    session: Session,
    predecessor_generation_id: uuid.UUID,
    *,
    contaminated_candidate_id: uuid.UUID,
    contaminated_cutover_run_id: uuid.UUID,
    contaminated_reservation_id: uuid.UUID,
) -> ContaminationEvidence:
    """Require the exact known fixture incident, never a generic first-admission state."""
    candidate = session.get(rel.ReleaseCandidate, contaminated_candidate_id)
    cutover = session.get(rel.CutoverRun, contaminated_cutover_run_id)
    reservation = session.get(reservations.FirstRequestReservation, contaminated_reservation_id)
    control = session.get(rel.MutationAdmissionControl, predecessor_generation_id)
    if candidate is None or cutover is None or reservation is None or control is None:
        raise GenerationRolloverError(
            "explicit contaminated candidate/cutover/reservation identity is incomplete"
        )
    if (
        candidate.generation_id != predecessor_generation_id
        or cutover.candidate_id != candidate.candidate_id
        or reservation.generation_id != predecessor_generation_id
        or reservation.candidate_id != candidate.candidate_id
        or reservation.cutover_run_id != cutover.cutover_run_id
        or control.candidate_id != candidate.candidate_id
    ):
        raise GenerationRolloverError(
            "explicit contaminated candidate/cutover/reservation identities do not bind one predecessor bundle"
        )
    if (
        candidate.status != "activated"
        or cutover.rehearsal_id is not None
        or cutover.state != "admission_open"
        or control.state != "closed"
        or reservation.state != "reserved"
    ):
        raise GenerationRolloverError(
            "explicit contaminated bundle is not in the known blocked first-admission lifecycle state"
        )

    plan = session.get(rel.FirstAdmissionPlan, reservation.plan_id)
    batch = session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
    if plan is None or batch is None:
        raise GenerationRolloverError("fixture-contamination provenance is incomplete")
    source_evidence = session.scalars(
        select(tx.SourceImportEntityEvidence)
        .where(tx.SourceImportEntityEvidence.import_batch_id == batch.import_batch_id)
        .order_by(tx.SourceImportEntityEvidence.entity_kind)
    ).all()

    expected_plan_payload = {
        "command_arguments": {
            "task_id": str(_FIXTURE_TASK_ID),
            "agent": "codex",
            "kind": "initial",
        },
        "operator_evidence": {"probe": "first production mutation"},
        "operation_id": None,
        "owner_id": "owner-1",
        "principal_class": "agent",
        "run_id": str(reservation.run_id),
        "canonical_payload_sha256": reservation.canonical_payload_sha256,
    }
    signature_ok = all(
        (
            candidate.source_release == _FIXTURE_SOURCE_RELEASE,
            candidate.source_commit == _FIXTURE_SOURCE_COMMIT,
            candidate.ledger_through_commit == _FIXTURE_SOURCE_COMMIT,
            candidate.source_manifest_sha256 == _FIXTURE_SHA256,
            candidate.rehearsal_environment_identity == _FIXTURE_REHEARSAL_ENVIRONMENT,
            candidate.dish_release == _FIXTURE_SOURCE_RELEASE,
            candidate.openapi_release == "openapi-stage4",
            candidate.routing_release == "routing-stage6",
            _as_utc(candidate.created_at) == _FIXTURE_TIMESTAMP,
            _as_utc(cutover.started_at) == _FIXTURE_TIMESTAMP.replace(minute=5),
            batch.generation_id == predecessor_generation_id,
            batch.source_release == _FIXTURE_SOURCE_RELEASE,
            batch.source_commit == _FIXTURE_SOURCE_COMMIT,
            batch.ledger_through_commit == _FIXTURE_SOURCE_COMMIT,
            batch.source_database_sha256 == _FIXTURE_SHA256,
            batch.source_sidecars == {"audit": {"sha256": _FIXTURE_SHA256}},
            batch.expected_entities == 4,
            batch.imported_entities == 4,
            batch.status == "complete",
            _as_utc(batch.started_at) == _FIXTURE_TIMESTAMP,
            _as_utc(batch.completed_at) == _FIXTURE_TIMESTAMP,
            len(source_evidence) == 4,
            all(row.source_sha256 == _FIXTURE_SHA256 for row in source_evidence),
            all(row.provenance == {"source": "stage6-fixture"} for row in source_evidence),
            all(_as_utc(row.imported_at) == _FIXTURE_TIMESTAMP for row in source_evidence),
            plan.cutover_run_id == cutover.cutover_run_id,
            plan.request_id == reservation.request_id,
            plan.command_name == "start",
            plan.task_id == _FIXTURE_TASK_ID,
            plan.expected_projection_events == 0,
            plan.payload == expected_plan_payload,
            _as_utc(plan.recorded_at) == _FIXTURE_TIMESTAMP.replace(minute=6),
            reservation.command_name == "start",
            reservation.owner_id == "owner-1",
            reservation.principal_class == "agent",
            reservation.reservation_revision == 1,
            _as_utc(reservation.reserved_at) == _FIXTURE_TIMESTAMP.replace(minute=6),
        )
    )
    if not signature_ok:
        raise GenerationRolloverError(
            "explicit Stage 6 bundle does not match the known fixture-contamination incident signature"
        )

    return ContaminationEvidence(
        candidate_id=candidate.candidate_id,
        cutover_run_id=cutover.cutover_run_id,
        reservation_id=reservation.reservation_id,
        shadow_baseline_id=candidate.shadow_baseline_id,
        projection_epoch_id=candidate.projection_epoch_id,
    )


def _active_registry_snapshot(
    session: Session,
    *,
    predecessor_generation_id: uuid.UUID,
    research_queue_section_id: uuid.UUID,
    verification_queue_section_id: uuid.UUID,
) -> tuple[models.SectionRegistryVersion, list[dict[str, Any]]]:
    if research_queue_section_id == verification_queue_section_id:
        raise GenerationRolloverError(
            "Research Queue and Verification Queue must be different sections"
        )
    current = session.get(models.ActiveSectionRegistry, predecessor_generation_id)
    if current is None:
        raise GenerationRolloverError("predecessor has no active section registry")
    version = session.get(models.SectionRegistryVersion, current.registry_version_id)
    if version is None or version.generation_id != predecessor_generation_id:
        raise GenerationRolloverError("predecessor registry provenance is inconsistent")
    entries = session.scalars(
        select(models.SectionRegistryEntry)
        .where(models.SectionRegistryEntry.registry_version_id == version.registry_version_id)
        .order_by(models.SectionRegistryEntry.ordinal)
    ).all()
    section_ids = {entry.section_id for entry in entries}
    for label, section_id in (
        ("research_queue", research_queue_section_id),
        ("verification_queue", verification_queue_section_id),
    ):
        if section_id not in section_ids:
            raise GenerationRolloverError(
                f"{label} section {section_id} is not present in the predecessor registry"
            )

    snapshot: list[dict[str, Any]] = []
    roles: set[str] = set()
    for entry in entries:
        if entry.section_id == research_queue_section_id:
            role = "research_queue"
        elif entry.section_id == verification_queue_section_id:
            role = "verification_queue"
        elif entry.workflow_role in _SPECIAL_ROLES:
            alias = session.scalar(
                select(models.SectionExternalAlias).where(
                    models.SectionExternalAlias.section_id == entry.section_id,
                    models.SectionExternalAlias.external_system == "asana",
                    models.SectionExternalAlias.state == "active",
                )
            )
            if alias is None:
                raise GenerationRolloverError(
                    f"cannot safely demote stale special role for section {entry.section_id}: active Asana alias missing"
                )
            role = f"imported-section-{alias.external_id}"
        else:
            role = entry.workflow_role
        if role in roles:
            raise GenerationRolloverError(
                f"corrected registry would contain duplicate workflow role {role!r}"
            )
        roles.add(role)
        snapshot.append(
            {
                "section_id": entry.section_id,
                "ordinal": entry.ordinal,
                "display_name": entry.display_name,
                "workflow_role": role,
            }
        )
    return version, snapshot


def _task_snapshot(session: Session, generation_id: uuid.UUID) -> list[dict[str, Any]]:
    states = session.scalars(
        select(models.DishState)
        .where(models.DishState.generation_id == generation_id)
        .order_by(models.DishState.task_id)
    ).all()
    result: list[dict[str, Any]] = []
    for state in states:
        task = session.get(models.DishTask, state.task_id)
        membership_head = session.get(models.TaskMembershipHead, (generation_id, state.task_id))
        if task is None or membership_head is None:
            raise GenerationRolloverError(
                f"predecessor task authority is incomplete for task {state.task_id}"
            )
        version = session.get(models.ContentVersion, state.current_content_version_id)
        if version is None:
            raise GenerationRolloverError(
                f"predecessor current task snapshot is incomplete for task {state.task_id}"
            )
        memberships = session.scalars(
            select(models.CurrentTaskProjectMembership)
            .where(
                models.CurrentTaskProjectMembership.generation_id == generation_id,
                models.CurrentTaskProjectMembership.task_id == state.task_id,
            )
            .order_by(models.CurrentTaskProjectMembership.project_id)
        ).all()
        result.append(
            {
                "task_id": state.task_id,
                "version": {
                    "representation_kind": version.representation_kind,
                    "title": version.title,
                    "body": version.body,
                    "identity_scheme": version.identity_scheme,
                    "content_identity": version.content_identity,
                    "contract_binding_id": version.contract_binding_id,
                },
                "memberships": [
                    {"project_id": row.project_id, "is_member": row.is_member}
                    for row in memberships
                ],
                "placement": {"section_id": state.section_id},
                "completion": {
                    "completed": state.completed,
                    "archived_at": state.archived_at,
                },
            }
        )
    return result


def _snapshot_digest(
    *,
    predecessor_generation_id: uuid.UUID,
    registry: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    contamination: ContaminationEvidence,
) -> str:
    return _canonical_sha256(
        {
            "format": "dish-test-generation-rollover-snapshot-v1",
            "predecessor_generation_id": str(predecessor_generation_id),
            "registry": registry,
            "tasks": tasks,
            "contamination": {
                "candidate_id": str(contamination.candidate_id),
                "cutover_run_id": str(contamination.cutover_run_id),
                "reservation_id": str(contamination.reservation_id),
            },
        }
    )


def _registry_sha256(session: Session, entries: list[dict[str, Any]]) -> str:
    sections: list[dict[str, Any]] = []
    project_ids: set[uuid.UUID] = set()
    for entry in entries:
        section = session.get(models.GovernedSection, entry["section_id"])
        if section is None or section.lifecycle != "active":
            raise GenerationRolloverError("rollover registry requires active governed sections")
        project_ids.add(section.project_id)
        alias = session.scalar(
            select(models.SectionExternalAlias).where(
                models.SectionExternalAlias.section_id == section.section_id,
                models.SectionExternalAlias.external_system == "asana",
                models.SectionExternalAlias.state == "active",
            )
        )
        if alias is None:
            raise GenerationRolloverError(
                f"rollover registry section {section.section_id} has no active Asana alias"
            )
        sections.append(
            {
                "section_id": str(section.section_id),
                "logical_name": section.logical_name,
                "display_name": entry["display_name"],
                "workflow_role": entry["workflow_role"],
                "ordinal": entry["ordinal"],
                "external_system": "asana",
                "external_id": alias.external_id,
            }
        )
    if len(project_ids) != 1:
        raise GenerationRolloverError("rollover registry must belong to exactly one governed project")
    project_id = next(iter(project_ids))
    project = session.get(models.GovernedProject, project_id)
    project_alias = session.scalar(
        select(models.ProjectExternalAlias).where(
            models.ProjectExternalAlias.project_id == project_id,
            models.ProjectExternalAlias.external_system == "asana",
            models.ProjectExternalAlias.state == "active",
        )
    )
    if project is None or project_alias is None:
        raise GenerationRolloverError("rollover registry project authority is incomplete")
    return _canonical_sha256(
        {
            "format": "dish-section-registry-v1",
            "project": {
                "project_id": str(project_id),
                "logical_name": project.logical_name,
                "external_system": "asana",
                "external_id": project_alias.external_id,
            },
            "sections": sections,
        }
    )


def _clone_task_authority(
    session: Session,
    *,
    generation_id: uuid.UUID,
    import_run_id: uuid.UUID,
    registry_version_id: uuid.UUID,
    tasks: list[dict[str, Any]],
    at: datetime,
    uuid_factory: Callable[[], uuid.UUID],
) -> None:
    for snapshot in tasks:
        task_id = snapshot["task_id"]
        version_id = uuid_factory()
        version_data = snapshot["version"]
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
                occurred_at=at,
            )
        )
        session.flush()
        session.add(
            models.ContentVersion(
                content_version_id=version_id,
                generation_id=generation_id,
                task_id=task_id,
                representation_kind=version_data["representation_kind"],
                title=version_data["title"],
                body=version_data["body"],
                identity_scheme=version_data["identity_scheme"],
                content_identity=version_data["content_identity"],
                creator_route="import",
                import_run_id=import_run_id,
                command_execution_id=None,
                predecessor_content_version_id=None,
                contract_binding_id=version_data["contract_binding_id"],
                created_dish_version=1,
                created_at=at,
            )
        )
        session.flush()
        memberships = snapshot["memberships"]
        session.add(
            models.DishState(
                generation_id=generation_id,
                task_id=task_id,
                current_content_version_id=version_id,
                section_id=snapshot["placement"]["section_id"],
                registry_version_id=registry_version_id,
                completed=snapshot["completion"]["completed"],
                completion_reason="imported",
                archived_at=snapshot["completion"]["archived_at"],
                dish_version=1,
                placement_version=1,
                completion_version=1,
                updated_at=at,
            )
        )
        session.add(
            models.TaskMembershipHead(
                generation_id=generation_id,
                task_id=task_id,
                membership_revision=1 if memberships else 0,
                updated_at=at,
            )
        )
        session.flush()
        for membership in memberships:
            event_id = uuid_factory()
            session.add(
                models.TaskProjectMembershipEvent(
                    membership_event_id=event_id,
                    generation_id=generation_id,
                    task_id=task_id,
                    project_id=membership["project_id"],
                    event_kind="joined" if membership["is_member"] else "left",
                    membership_revision=1,
                    provenance_route="import",
                    import_run_id=import_run_id,
                    command_execution_id=None,
                    occurred_at=at,
                )
            )
            session.flush()
            session.add(
                models.CurrentTaskProjectMembership(
                    generation_id=generation_id,
                    task_id=task_id,
                    project_id=membership["project_id"],
                    latest_event_id=event_id,
                    is_member=membership["is_member"],
                    membership_revision=1,
                    updated_at=at,
                )
            )
        session.flush()


def rollover_test_generation(
    session: Session,
    *,
    predecessor_generation_id: uuid.UUID,
    contaminated_candidate_id: uuid.UUID,
    contaminated_cutover_run_id: uuid.UUID,
    contaminated_reservation_id: uuid.UUID,
    research_queue_section_id: uuid.UUID,
    verification_queue_section_id: uuid.UUID,
    source_commit: str,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    failure_hook: Callable[[], None] | None = None,
) -> GenerationRolloverResult:
    """Atomically retire one contaminated TEST generation and activate its clean successor.

    The caller owns the transaction. The connected database identity is always read directly
    from PostgreSQL; callers cannot supply or infer it. No predecessor Stage 6, reservation,
    baseline, epoch, envelope, comparison, or gap row is updated or deleted.
    """
    _require_test_database_name(_connected_database_name(session))
    return _rollover_generation_transaction(
        session,
        predecessor_generation_id=predecessor_generation_id,
        contaminated_candidate_id=contaminated_candidate_id,
        contaminated_cutover_run_id=contaminated_cutover_run_id,
        contaminated_reservation_id=contaminated_reservation_id,
        research_queue_section_id=research_queue_section_id,
        verification_queue_section_id=verification_queue_section_id,
        source_commit=source_commit,
        uuid_factory=uuid_factory,
        clock=clock,
        failure_hook=failure_hook,
    )


def _rollover_generation_transaction(
    session: Session,
    *,
    predecessor_generation_id: uuid.UUID,
    contaminated_candidate_id: uuid.UUID,
    contaminated_cutover_run_id: uuid.UUID,
    contaminated_reservation_id: uuid.UUID,
    research_queue_section_id: uuid.UUID,
    verification_queue_section_id: uuid.UUID,
    source_commit: str,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    failure_hook: Callable[[], None] | None = None,
) -> GenerationRolloverResult:
    if not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise GenerationRolloverError("source_commit must be an exact 40-character lowercase Git SHA")
    now = clock()
    _require_aware(now)

    predecessor = session.scalar(
        select(models.AuthorityGeneration)
        .where(models.AuthorityGeneration.generation_id == predecessor_generation_id)
        .with_for_update()
    )
    if predecessor is None:
        raise GenerationRolloverError("explicit predecessor generation does not exist")
    if predecessor.status != "active":
        raise GenerationRolloverError("explicit predecessor generation must be status='active'")
    active_ids = session.scalars(
        select(models.AuthorityGeneration.generation_id).where(
            models.AuthorityGeneration.status == "active"
        )
    ).all()
    if active_ids != [predecessor_generation_id]:
        raise GenerationRolloverError("explicit predecessor must be the one active authority generation")

    contamination = _contamination_evidence(
        session,
        predecessor_generation_id,
        contaminated_candidate_id=contaminated_candidate_id,
        contaminated_cutover_run_id=contaminated_cutover_run_id,
        contaminated_reservation_id=contaminated_reservation_id,
    )
    old_baseline = session.get(tx.ShadowBaseline, contamination.shadow_baseline_id)
    old_epoch = session.get(tx.ProjectionEpoch, contamination.projection_epoch_id)
    if (
        old_baseline is None
        or old_baseline.generation_id != predecessor_generation_id
        or old_epoch is None
        or old_epoch.generation_id != predecessor_generation_id
    ):
        raise GenerationRolloverError("contaminated candidate dark-launch provenance is inconsistent")

    source_registry, registry_entries = _active_registry_snapshot(
        session,
        predecessor_generation_id=predecessor_generation_id,
        research_queue_section_id=research_queue_section_id,
        verification_queue_section_id=verification_queue_section_id,
    )
    tasks = _task_snapshot(session, predecessor_generation_id)
    snapshot_sha256 = _snapshot_digest(
        predecessor_generation_id=predecessor_generation_id,
        registry=registry_entries,
        tasks=tasks,
        contamination=contamination,
    )
    registry_sha256 = _registry_sha256(session, registry_entries)

    generation_id = uuid_factory()
    import_run_id = uuid_factory()
    registry_version_id = uuid_factory()
    registry_activation_id = uuid_factory()
    receipt = {
        "format": "dish-test-generation-rollover-receipt-v1",
        "reason": ROLLOVER_REASON,
        "predecessor_generation_id": str(predecessor_generation_id),
        "generation_id": str(generation_id),
        "source_commit": source_commit,
        "timestamp": now.isoformat(),
        "contaminated_stage6": {
            "candidate_id": str(contamination.candidate_id),
            "cutover_run_id": str(contamination.cutover_run_id),
            "reservation_id": str(contamination.reservation_id),
            "rows_preserved_in_place": True,
        },
        "snapshot_sha256": snapshot_sha256,
        "task_count": len(tasks),
        "stable_external_aliases_reused": True,
        "research_queue_section_id": str(research_queue_section_id),
        "verification_queue_section_id": str(verification_queue_section_id),
    }
    session.add(
        models.ImportRun(
            import_run_id=import_run_id,
            source_commit=source_commit,
            source_release=predecessor.dish_release,
            legacy_generation_id=f"test-rollover:{predecessor_generation_id}",
            baseline_high_water_mark=f"test-fixture-recovery:{generation_id}",
            source_bundle_sha256=snapshot_sha256,
            status="complete",
            started_at=now,
            completed_at=now,
            provenance=receipt,
        )
    )
    session.flush()
    session.add(
        models.AuthorityGeneration(
            generation_id=generation_id,
            predecessor_generation_id=predecessor_generation_id,
            creation_reason=CREATION_REASON,
            external_restore_control_id=None,
            schema_head=ALEMBIC_HEAD,
            dish_release=predecessor.dish_release,
            status="pending",
            created_at=now,
            retired_at=None,
        )
    )
    session.flush()

    registry_repo = RegistryRepository(session)
    registry_repo.add_registry_version(
        models.SectionRegistryVersion(
            registry_version_id=registry_version_id,
            generation_id=generation_id,
            version_number=1,
            import_run_id=import_run_id,
            contract_binding_id=source_registry.contract_binding_id,
            registry_sha256=registry_sha256,
            created_at=now,
        ),
        [
            models.SectionRegistryEntry(
                registry_version_id=registry_version_id,
                section_id=entry["section_id"],
                ordinal=entry["ordinal"],
                display_name=entry["display_name"],
                workflow_role=entry["workflow_role"],
            )
            for entry in registry_entries
        ],
    )
    registry_repo.activate_registry(
        activation=models.SectionRegistryActivation(
            registry_activation_id=registry_activation_id,
            generation_id=generation_id,
            registry_version_id=registry_version_id,
            activation_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            registry_revision=1,
            activated_at=now,
        ),
        current=models.ActiveSectionRegistry(
            generation_id=generation_id,
            registry_version_id=registry_version_id,
            registry_activation_id=registry_activation_id,
            registry_revision=1,
            updated_at=now,
        ),
    )
    _clone_task_authority(
        session,
        generation_id=generation_id,
        import_run_id=import_run_id,
        registry_version_id=registry_version_id,
        tasks=tasks,
        at=now,
        uuid_factory=uuid_factory,
    )

    predecessor.status = "retired"
    predecessor.retired_at = now
    session.flush()
    successor = session.get(models.AuthorityGeneration, generation_id)
    if successor is None or successor.status != "pending":
        raise GenerationRolloverError("successor generation disappeared before activation")
    successor.status = "active"
    session.flush()
    if failure_hook is not None:
        failure_hook()

    new_baseline = ShadowService(session, uuid_factory=uuid_factory).create_baseline(
        generation_id=generation_id,
        source_generation_identity=old_baseline.source_generation_identity,
        source_commit=old_baseline.source_commit,
        created_at=now,
    )
    new_epoch = ProjectionService(session, uuid_factory=uuid_factory).activate_epoch(
        generation_id=generation_id,
        activation_reason=ROLLOVER_REASON,
        created_at=now,
        external_effects_enabled=False,
    )
    return GenerationRolloverResult(
        predecessor_generation_id=predecessor_generation_id,
        generation_id=generation_id,
        import_run_id=import_run_id,
        registry_version_id=registry_version_id,
        registry_activation_id=registry_activation_id,
        shadow_baseline_id=new_baseline.shadow_baseline_id,
        projection_epoch_id=new_epoch.projection_epoch_id,
        contamination=contamination,
        source_commit=source_commit,
        snapshot_sha256=snapshot_sha256,
        task_count=len(tasks),
        rolled_over_at=now,
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, sort_keys=True, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-test-generation-rollover")
    parser.add_argument("--predecessor-generation-id", type=uuid.UUID, required=True)
    parser.add_argument("--contaminated-candidate-id", type=uuid.UUID, required=True)
    parser.add_argument("--contaminated-cutover-run-id", type=uuid.UUID, required=True)
    parser.add_argument("--contaminated-reservation-id", type=uuid.UUID, required=True)
    parser.add_argument("--research-queue-section-id", type=uuid.UUID, required=True)
    parser.add_argument("--verification-queue-section-id", type=uuid.UUID, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--receipt", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get("DISH_PG_DATABASE_URL", "")
    try:
        require_test_database_url(database_url)
        require_git_head(Path(__file__).resolve().parents[2], args.source_commit)
        engine = create_database_engine(DatabaseSettings(url=database_url))
        try:
            factory = session_factory(engine)
            with session_scope(factory) as session:
                result = rollover_test_generation(
                    session,
                    predecessor_generation_id=args.predecessor_generation_id,
                    contaminated_candidate_id=args.contaminated_candidate_id,
                    contaminated_cutover_run_id=args.contaminated_cutover_run_id,
                    contaminated_reservation_id=args.contaminated_reservation_id,
                    research_queue_section_id=args.research_queue_section_id,
                    verification_queue_section_id=args.verification_queue_section_id,
                    source_commit=args.source_commit,
                )
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - operator-facing fail-closed receipt
        report = {"ok": False, "error": str(exc), "type": type(exc).__name__}
        if args.receipt is not None:
            _atomic_json(args.receipt, report)
        print(json.dumps(report, sort_keys=True))
        return 2
    receipt = result.as_dict()
    if args.receipt is not None:
        _atomic_json(args.receipt, receipt)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
